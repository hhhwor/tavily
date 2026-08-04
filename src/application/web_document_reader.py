"""Secure original-page reader with paragraph locators and version hashes."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from src.application.document_identity import web_document_version_id
from src.application.research_execution import ExecutionContext
from src.domain.document_read import (
    DocumentChunk,
    DocumentReadDiagnostics,
    DocumentReadResult,
    DocumentVersion,
)
from src.domain.evidence import Evidence, EvidenceLocator
from src.infrastructure.safe_web_fetch import SafeWebFetchError, SafeWebFetcher


_PARSER_VERSION = "web-html-parser.v1"
_CHARSET = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.I)
_SPACE = re.compile(r"\s+")
_BLOCKS = {
    "p", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"
}
_SKIP = {"script", "style", "noscript", "svg", "canvas", "nav", "aside", "footer"}


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[str, str | None, str]] = []
        self.canonical_url: str | None = None
        self.license_url: str | None = None
        self.robots: set[str] = set()
        self._skip_depth = 0
        self._tag: str | None = None
        self._paragraph_id: str | None = None
        self._parts: list[str] = []
        self._section: str | None = None
        self._counter = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        tag = tag.casefold()
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "link":
            rel = set(values.get("rel", "").casefold().split())
            if "canonical" in rel and values.get("href"):
                self.canonical_url = values["href"]
            if "license" in rel and values.get("href"):
                self.license_url = values["href"]
            return
        if tag == "meta" and values.get("name", "").casefold() in {
            "robots", "googlebot"
        }:
            self.robots.update(
                token.strip().casefold()
                for token in values.get("content", "").split(",")
                if token.strip()
            )
            return
        if tag in _BLOCKS:
            self._flush()
            self._tag = tag
            self._paragraph_id = values.get("id") or None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if self._tag == tag:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._tag is not None:
            self._parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._tag is None:
            self._parts = []
            return
        text = _SPACE.sub(" ", " ".join(self._parts)).strip()
        tag = self._tag
        paragraph_id = self._paragraph_id
        self._tag = None
        self._paragraph_id = None
        self._parts = []
        if not text:
            return
        if tag.startswith("h"):
            self._section = text
            return
        self._counter += 1
        self.paragraphs.append((
            paragraph_id or f"p-{self._counter}",
            self._section,
            text,
        ))


class WebDocumentReader:
    def __init__(
        self,
        fetcher: SafeWebFetcher,
        *,
        max_paragraphs: int = 500,
        max_paragraph_chars: int = 10_000,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._max_paragraphs = max(1, max_paragraphs)
        self._max_paragraph_chars = max(1, max_paragraph_chars)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def read(
        self,
        candidate: Evidence,
        *,
        context: ExecutionContext,
    ) -> DocumentReadResult:
        if candidate.type != "web":
            return self._failure("DOCUMENT_TYPE_UNSUPPORTED", retryable=False)
        if not candidate.url:
            return self._failure("WEB_URL_MISSING", retryable=False)
        warnings: list[str] = []
        try:
            allowed, robots_warning = self._robots_allowed(
                candidate.url,
                context,
            )
            if robots_warning:
                warnings.append(robots_warning)
            if not allowed:
                return self._failure("ROBOTS_DISALLOWED", retryable=False)
            context.checkpoint()
            response = self._fetcher.fetch(
                candidate.url,
                deadline=context.deadline,
            )
        except SafeWebFetchError as exc:
            return self._failure(exc.code, retryable=exc.retryable)
        if not 200 <= response.status < 300:
            return self._failure(
                f"WEB_HTTP_{response.status}",
                retryable=response.status in {408, 425, 429} or response.status >= 500,
            )

        mime = response.headers.get("content-type", "").split(";", 1)[0]
        try:
            text = self._decode(response.body, response.headers.get("content-type", ""))
        except (LookupError, UnicodeError):
            return self._failure("WEB_CHARSET_INVALID", retryable=False)
        if mime.casefold() == "text/plain":
            paragraphs = [
                (f"p-{index}", None, value.strip())
                for index, value in enumerate(re.split(r"\n\s*\n", text), start=1)
                if value.strip()
            ]
            parser = None
        else:
            parser = _ArticleParser()
            try:
                parser.feed(text)
                parser.close()
            except Exception:
                return self._failure("WEB_HTML_PARSE_FAILED", retryable=False)
            paragraphs = parser.paragraphs
        if not paragraphs:
            return self._failure("WEB_ORIGINAL_TEXT_EMPTY", retryable=False)

        canonical_url = response.url
        license_url = None
        robots_tokens: set[str] = set()
        if parser is not None:
            canonical_url = self._canonical(response.url, parser.canonical_url)
            license_url = (
                urljoin(response.url, parser.license_url)
                if parser.license_url else None
            )
            robots_tokens = parser.robots
        header_robots = {
            token.strip().casefold()
            for token in response.headers.get("x-robots-tag", "").split(",")
            if token.strip()
        }
        robots_tokens.update(header_robots)
        tdm_reserved = response.headers.get(
            "tdm-reservation", ""
        ).strip().casefold() in {"1", "true", "yes"}
        storage_mode = "full_text"
        if robots_tokens & {"noarchive", "none"} or tdm_reserved:
            storage_mode = "locator_only"
            warnings.append("WEB_STORAGE_NOT_PERMITTED")

        content_hash = _sha256_bytes(response.body)
        extracted_hash = _sha256_bytes(
            "\n".join(paragraph for _, _, paragraph in paragraphs)
            .encode("utf-8")
        )
        version_id = web_document_version_id(canonical_url, content_hash)
        retrieved_at = self._now()
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
        version = DocumentVersion(
            document_version_id=version_id,
            independent_work_id=f"web-content:{extracted_hash}",
            type="web",
            source_record_id=canonical_url,
            canonical_uri=canonical_url,
            content_hash=content_hash,
            content_hash_scope="full_document",
            parser_version=_PARSER_VERSION,
            retrieved_at=retrieved_at.astimezone(timezone.utc).isoformat(),
            complete=True,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            license=candidate.access.license or license_url,
            storage_mode=storage_mode,
        )
        chunks: list[DocumentChunk] = []
        for index, (paragraph_id, section, paragraph) in enumerate(
            paragraphs[:self._max_paragraphs]
        ):
            clipped = paragraph[:self._max_paragraph_chars]
            if len(paragraph) > len(clipped):
                warnings.append("WEB_PARAGRAPH_TRUNCATED")
            chunks.append(DocumentChunk(
                document_version_id=version_id,
                chunk_index=index,
                text=clipped,
                text_hash=_sha256_bytes(clipped.encode("utf-8")),
                locator=EvidenceLocator(
                    document_id=canonical_url,
                    version_id=version_id,
                    section=section,
                    paragraph_id=paragraph_id,
                    char_start=0,
                    char_end=len(clipped),
                    chunk_index=index,
                ),
            ))
        if len(paragraphs) > self._max_paragraphs:
            warnings.append("WEB_PARAGRAPH_LIMIT_EXCEEDED")
        return DocumentReadResult(
            status="ready",
            version=version,
            chunks=chunks,
            diagnostics=DocumentReadDiagnostics(
                warnings=list(dict.fromkeys(warnings)),
                message="Web original page read completed.",
            ),
            bytes_read=len(response.body),
        )

    def _robots_allowed(
        self,
        url: str,
        context: ExecutionContext,
    ) -> tuple[bool, str | None]:
        parsed = urlsplit(url)
        robots_url = urlunsplit((
            parsed.scheme,
            parsed.netloc,
            "/robots.txt",
            "",
            "",
        ))
        try:
            response = self._fetcher.fetch(
                robots_url,
                deadline=context.deadline,
                allowed_mime={"text/plain", "text/html"},
            )
        except SafeWebFetchError:
            return True, "ROBOTS_UNAVAILABLE"
        if response.status >= 400:
            return True, None
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            lines = self._decode(
                response.body,
                response.headers.get("content-type", ""),
            ).splitlines()
            parser.parse(lines)
        except (LookupError, UnicodeError, ValueError):
            return True, "ROBOTS_PARSE_FAILED"
        return parser.can_fetch("TavilyResearchBot", url), None

    @staticmethod
    def _decode(body: bytes, content_type: str) -> str:
        match = _CHARSET.search(content_type)
        charset = match.group(1) if match else "utf-8"
        return body.decode(charset, errors="strict")

    @staticmethod
    def _canonical(base_url: str, value: str | None) -> str:
        if not value:
            return base_url
        candidate = urljoin(base_url, value.strip())
        parsed = urlsplit(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return base_url
        return candidate

    @staticmethod
    def _failure(code: str, *, retryable: bool) -> DocumentReadResult:
        return DocumentReadResult(
            status="failed" if retryable else "unavailable",
            diagnostics=DocumentReadDiagnostics(
                warnings=[code],
                failure_code=code,
                message="Web original page is unavailable.",
                retryable=retryable,
            ),
        )
