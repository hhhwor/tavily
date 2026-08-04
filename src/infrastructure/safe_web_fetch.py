"""SSRF-resistant, redirect-aware HTTP transport for original web pages."""
from __future__ import annotations

import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from urllib3.connection import HTTPConnection, HTTPSConnection
from src.application.ports.runtime import Deadline


_REDIRECTS = {301, 302, 303, 307, 308}
_ALLOWED_MIME = {"text/html", "text/plain", "application/xhtml+xml"}


class SafeWebFetchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SafeHttpResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    compressed_bytes: int


class WebHttpTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        resolved_ip: str,
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> SafeHttpResponse: ...


class PinnedIpHttpTransport:
    """Connect to a validated IP while preserving HTTP Host and TLS SNI."""

    def __init__(
        self,
        *,
        max_compressed_bytes: int = 2_000_000,
        max_decoded_bytes: int = 5_000_000,
        max_compression_ratio: float = 100.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_compressed_bytes = max(1, max_compressed_bytes)
        self._max_decoded_bytes = max(1, max_decoded_bytes)
        self._max_compression_ratio = max(1.0, max_compression_ratio)
        self._monotonic = monotonic

    def request(
        self,
        url: str,
        *,
        resolved_ip: str,
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> SafeHttpResponse:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        timeout = max(0.1, timeout_seconds)
        if parsed.scheme == "https":
            connection = HTTPSConnection(
                resolved_ip,
                port,
                timeout=timeout,
                ssl_context=ssl.create_default_context(),
                server_hostname=host,
                assert_hostname=host,
            )
        else:
            connection = HTTPConnection(
                resolved_ip,
                port,
                timeout=timeout,
            )
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request_headers = dict(headers)
        default_port = 443 if parsed.scheme == "https" else 80
        host_header = f"[{host}]" if ":" in host else host
        request_headers["Host"] = (
            host_header
            if port == default_port
            else f"{host_header}:{port}"
        )
        expires_at = self._monotonic() + timeout
        response = None
        try:
            connection.request(
                "GET",
                path,
                headers=request_headers,
                preload_content=False,
                decode_content=False,
            )
            response = connection.getresponse()
            raw_headers = [
                (str(key), str(value))
                for key, value in response.headers.items()
            ]
            if sum(
                len(key) + len(value)
                for key, value in raw_headers
            ) > 65_536:
                raise SafeWebFetchError(
                    "WEB_HEADERS_TOO_LARGE",
                    "Web response headers exceed the configured limit.",
                )
            normalized_headers = {
                key.casefold(): value for key, value in raw_headers
            }
            if response.status in _REDIRECTS:
                return SafeHttpResponse(
                    url=url,
                    status=response.status,
                    headers=normalized_headers,
                    body=b"",
                    compressed_bytes=0,
                )
            try:
                body, compressed = self._read_body(
                    response,
                    normalized_headers,
                    expires_at=expires_at,
                )
            except zlib.error as exc:
                raise SafeWebFetchError(
                    "WEB_COMPRESSION_INVALID",
                    "Compressed web response is invalid.",
                ) from exc
            return SafeHttpResponse(
                url=url,
                status=response.status,
                headers=normalized_headers,
                body=body,
                compressed_bytes=compressed,
            )
        except SafeWebFetchError:
            raise
        except (OSError, TimeoutError) as exc:
            raise SafeWebFetchError(
                "WEB_FETCH_FAILED",
                "Web original-page request failed.",
                retryable=True,
            ) from exc
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _read_body(
        self,
        response,
        headers: Mapping[str, str],
        *,
        expires_at: float | None = None,
    ) -> tuple[bytes, int]:
        length = headers.get("content-length")
        if length:
            try:
                parsed_length = int(length)
                if parsed_length < 0:
                    raise ValueError("negative Content-Length")
                if parsed_length > self._max_compressed_bytes:
                    raise SafeWebFetchError(
                        "WEB_BODY_TOO_LARGE",
                        "Web response body exceeds the configured limit.",
                    )
            except ValueError as exc:
                raise SafeWebFetchError(
                    "WEB_CONTENT_LENGTH_INVALID",
                    "Web response Content-Length is invalid.",
                ) from exc
        encoding = headers.get("content-encoding", "").casefold().strip()
        if encoding in {"", "identity"}:
            decoder = None
        elif encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
        else:
            raise SafeWebFetchError(
                "WEB_CONTENT_ENCODING_UNSUPPORTED",
                "Web response Content-Encoding is unsupported.",
            )

        output: list[bytes] = []
        compressed = 0
        decoded = 0

        def append_decoded(data: bytes) -> None:
            nonlocal decoded
            decoded += len(data)
            if decoded > self._max_decoded_bytes:
                raise SafeWebFetchError(
                    "WEB_DECODED_BODY_TOO_LARGE",
                    "Decoded web response exceeds the configured limit.",
                )
            output.append(data)
            if decoded / max(1, compressed) > self._max_compression_ratio:
                raise SafeWebFetchError(
                    "WEB_COMPRESSION_RATIO_EXCEEDED",
                    "Web response compression ratio exceeds the limit.",
                )

        while True:
            if expires_at is not None:
                remaining = expires_at - self._monotonic()
                if remaining <= 0:
                    raise SafeWebFetchError(
                        "WEB_FETCH_DEADLINE_EXCEEDED",
                        "Web original-page deadline exceeded.",
                        retryable=True,
                    )
                connection = getattr(response, "_connection", None)
                sock = getattr(connection, "sock", None)
                if sock is not None:
                    sock.settimeout(max(0.1, remaining))
            raw = response.read(65_536, decode_content=False)
            if not raw:
                break
            compressed += len(raw)
            if compressed > self._max_compressed_bytes:
                raise SafeWebFetchError(
                    "WEB_BODY_TOO_LARGE",
                    "Web response body exceeds the configured limit.",
                )
            if decoder is None:
                append_decoded(raw)
                continue
            pending = raw
            while pending:
                data = decoder.decompress(
                    pending,
                    self._max_decoded_bytes - decoded + 1,
                )
                append_decoded(data)
                next_pending = decoder.unconsumed_tail
                if next_pending == pending and not data:
                    raise SafeWebFetchError(
                        "WEB_COMPRESSION_INVALID",
                        "Compressed web response made no decoding progress.",
                    )
                pending = next_pending
        if decoder is not None:
            tail = decoder.flush(self._max_decoded_bytes - decoded + 1)
            append_decoded(tail)
            if not decoder.eof:
                raise SafeWebFetchError(
                    "WEB_COMPRESSION_INVALID",
                    "Compressed web response ended before the stream completed.",
                )
        return b"".join(output), compressed


class SafeWebFetcher:
    def __init__(
        self,
        *,
        transport: WebHttpTransport | None = None,
        resolver: Callable[..., Sequence[tuple]] = socket.getaddrinfo,
        max_redirects: int = 5,
        request_timeout_seconds: float = 10.0,
        user_agent: str = "TavilyResearchBot/1.0",
    ) -> None:
        self._transport = transport or PinnedIpHttpTransport()
        self._resolver = resolver
        self._max_redirects = max(0, max_redirects)
        self._request_timeout_seconds = max(0.1, request_timeout_seconds)
        self._user_agent = user_agent

    def fetch(
        self,
        url: str,
        *,
        deadline: Deadline,
        allowed_mime: set[str] | None = None,
    ) -> SafeHttpResponse:
        current = url
        visited: set[str] = set()
        for redirect_count in range(self._max_redirects + 1):
            parsed = self._validate_url(current)
            normalized = parsed.geturl()
            if normalized in visited:
                raise SafeWebFetchError(
                    "WEB_REDIRECT_LOOP",
                    "Web redirect loop detected.",
                )
            visited.add(normalized)
            addresses = self._resolve_global_addresses(
                parsed.hostname or "",
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
            remaining = deadline.remaining_seconds()
            if remaining <= 0:
                raise SafeWebFetchError(
                    "WEB_FETCH_DEADLINE_EXCEEDED",
                    "Web original-page deadline exceeded.",
                    retryable=True,
                )
            response = self._transport.request(
                normalized,
                resolved_ip=addresses[0],
                timeout_seconds=min(self._request_timeout_seconds, remaining),
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "close",
                },
            )
            if response.status not in _REDIRECTS:
                mime = response.headers.get(
                    "content-type", ""
                ).split(";", 1)[0].casefold().strip()
                accepted = allowed_mime or _ALLOWED_MIME
                if mime not in accepted:
                    raise SafeWebFetchError(
                        "WEB_MIME_UNSUPPORTED",
                        "Web response MIME type is unsupported.",
                    )
                return SafeHttpResponse(
                    url=normalized,
                    status=response.status,
                    headers=response.headers,
                    body=response.body,
                    compressed_bytes=response.compressed_bytes,
                )
            location = response.headers.get("location", "").strip()
            if not location:
                raise SafeWebFetchError(
                    "WEB_REDIRECT_LOCATION_MISSING",
                    "Web redirect response has no Location header.",
                )
            if redirect_count >= self._max_redirects:
                raise SafeWebFetchError(
                    "WEB_REDIRECT_LIMIT_EXCEEDED",
                    "Web redirect limit exceeded.",
                )
            current = urljoin(normalized, location)
        raise AssertionError("redirect loop must terminate")

    @staticmethod
    def _validate_url(url: str):
        try:
            parsed = urlsplit((url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise SafeWebFetchError(
                "WEB_URL_INVALID", "Web URL is invalid."
            ) from exc
        if parsed.scheme not in {"http", "https"}:
            raise SafeWebFetchError(
                "WEB_SCHEME_UNSUPPORTED",
                "Only HTTP and HTTPS original pages are allowed.",
            )
        if not parsed.hostname or parsed.username or parsed.password:
            raise SafeWebFetchError(
                "WEB_URL_INVALID", "Web URL authority is invalid."
            )
        effective_port = port or (443 if parsed.scheme == "https" else 80)
        if effective_port not in {80, 443}:
            raise SafeWebFetchError(
                "WEB_PORT_UNSUPPORTED",
                "Web original-page port is not allowed.",
            )
        return parsed

    def _resolve_global_addresses(self, host: str, port: int) -> list[str]:
        try:
            rows = self._resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise SafeWebFetchError(
                "WEB_DNS_FAILED",
                "Web original-page DNS lookup failed.",
                retryable=True,
            ) from exc
        addresses = list(dict.fromkeys(
            str(row[4][0]) for row in rows if row and len(row) >= 5
        ))
        if not addresses:
            raise SafeWebFetchError(
                "WEB_DNS_FAILED",
                "Web original-page DNS lookup returned no address.",
                retryable=True,
            )
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise SafeWebFetchError(
                    "WEB_DNS_ADDRESS_INVALID",
                    "Web DNS response contained an invalid IP address.",
                ) from exc
            if not address.is_global:
                raise SafeWebFetchError(
                    "WEB_SSRF_ADDRESS_BLOCKED",
                    "Web URL resolved to a non-public address.",
                )
        return addresses
