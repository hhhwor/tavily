"""Aliyun Model Studio AI Search ``WebSearch`` provider."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import requests

from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.domain.errors import ExternalServiceError
from src.domain.search import SearchResult
from src.infrastructure.http_errors import external_http_error
from src.providers.base import SearchProvider


_ENDPOINT = "https://maasaisearchproxy.aliyuncs.com/api/web-search"
_ACTION = "WebSearch"
_VERSION = "2026-04-24"
_ALGORITHM = "ACS3-HMAC-SHA256"
_RECENCY_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    normalized = {
        str(key).lower(): str(value).strip()
        for key, value in headers.items()
        if value is not None
    }
    names = sorted(normalized)
    canonical = "".join(f"{name}:{normalized[name]}\n" for name in names)
    return canonical, ";".join(names)


def _authorization(
    *,
    method: str,
    pathname: str,
    headers: Mapping[str, str],
    payload_hash: str,
    access_key_id: str,
    access_key_secret: str,
) -> str:
    canonical_headers, signed_headers = _canonical_headers(headers)
    canonical_request = (
        f"{method}\n{pathname}\n\n{canonical_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"{_ALGORITHM}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signature = hmac.new(
        access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"{_ALGORITHM} Credential={access_key_id},"
        f"SignedHeaders={signed_headers},Signature={signature}"
    )


class AliyunWebSearchProvider(SearchProvider):
    """Expose Aliyun WebSearch Pro/Lite as a concurrent web recall source."""

    name = "aliyun"
    descriptor = SourceDescriptor(
        id=name,
        kind="web",
        capabilities=frozenset({
            "recency_filter",
            "time_range_filter",
            "snippet",
        }),
        default_snapshot=f"aliyun-web-search:{_VERSION}",
        data_license="aliyun-model-studio-terms",
        max_candidates=50,
        count_empty_as_used=True,
    )

    def __init__(
        self,
        access_key_id: Optional[str] = None,
        access_key_secret: Optional[str] = None,
        timeout: int = 15,
        *,
        search_type: str = "pro",
        region: str = "global",
        http_session: Optional[requests.Session] = None,
    ) -> None:
        self.access_key_id = access_key_id or ""
        self.access_key_secret = access_key_secret or ""
        self.timeout = timeout
        self.search_type = search_type
        self.region = region
        self._http = http_session or requests
        if not self.access_key_id or not self.access_key_secret:
            raise ValueError(
                "缺少阿里云凭证: ALIBABA_CLOUD_ACCESS_KEY_ID / "
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
            )
        if self.search_type not in {"pro", "lite"}:
            raise ValueError("ALIYUN_WEB_SEARCH_TYPE 仅支持 pro / lite")
        if self.region not in {"global", "mainland_china"}:
            raise ValueError(
                "ALIYUN_WEB_SEARCH_REGION 仅支持 global / mainland_china"
            )

    @staticmethod
    def _time_window(
        recency: str | None,
        request: RetrievalRequest | None,
    ) -> tuple[str | None, str | None]:
        if request is not None and (request.time_from or request.time_to):
            start = (
                request.time_from.date().isoformat()
                if request.time_from is not None
                else None
            )
            end = (
                request.time_to.date().isoformat()
                if request.time_to is not None
                else None
            )
            return start, end
        days = _RECENCY_DAYS.get(recency or "")
        if days is None:
            return None, None
        today = datetime.now(timezone.utc).date()
        return (today - timedelta(days=days)).isoformat(), today.isoformat()

    def actual_filters(self, request: RetrievalRequest) -> Mapping[str, Any]:
        start, end = self._time_window(request.recency, request)
        filters: dict[str, Any] = {
            "limit": min(max(request.candidate_budget, 1), 50),
            "searchType": self.search_type,
            "region": self.region,
        }
        if start:
            filters["startTime"] = start
        if end:
            filters["endTime"] = end
        return filters

    def applied_request_filters(
        self,
        request: RetrievalRequest,
    ) -> Mapping[str, Any]:
        start, end = self._time_window(request.recency, request)
        applied: dict[str, Any] = {}
        if start:
            applied["published_from"] = start
        if end:
            applied["published_to"] = end
        return applied

    def search(
        self,
        query: str,
        top_k: int = 10,
        recency: Optional[str] = None,
    ) -> list[SearchResult]:
        return self._search(query, top_k, recency, request=None)

    def search_request(
        self,
        request: RetrievalRequest,
    ) -> list[SearchResult]:
        return self._search(
            request.query,
            request.candidate_budget,
            request.recency,
            request=request,
        )

    def _search(
        self,
        query: str,
        top_k: int,
        recency: str | None,
        *,
        request: RetrievalRequest | None,
    ) -> list[SearchResult]:
        limit = min(max(top_k, 1), 50)
        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "searchType": self.search_type,
            "region": self.region,
        }
        start, end = self._time_window(recency, request)
        if start:
            body["startTime"] = start
        if end:
            body["endTime"] = end
        payload = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        parsed = urlsplit(_ENDPOINT)
        headers = {
            "host": parsed.netloc,
            "content-type": "application/json; charset=utf-8",
            "x-acs-action": _ACTION,
            "x-acs-content-sha256": hashlib.sha256(payload).hexdigest(),
            "x-acs-date": _utc_timestamp(),
            "x-acs-signature-nonce": str(uuid.uuid4()),
            "x-acs-version": _VERSION,
        }
        headers["authorization"] = _authorization(
            method="POST",
            pathname=parsed.path,
            headers=headers,
            payload_hash=headers["x-acs-content-sha256"],
            access_key_id=self.access_key_id,
            access_key_secret=self.access_key_secret,
        )
        try:
            response = self._http.post(
                _ENDPOINT,
                headers=headers,
                data=payload,
                timeout=self.request_timeout(request),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise external_http_error(self.name, "search", exc) from exc

        code = data.get("code")
        rows = (data.get("data") or {}).get("result")
        if str(code) not in {"0", "200", "None"} or not isinstance(rows, list):
            cause = RuntimeError(
                f"Aliyun WebSearch rejected response with code {code!r}"
            )
            raise ExternalServiceError(
                provider=self.name,
                code="SEARCH_UPSTREAM_REJECTED",
                recoverable=str(code) in {"429", "500", "503"},
                cause=cause,
            ) from cause
        return self._normalize(rows)[:limit]

    def _normalize(self, rows: list[dict[str, Any]]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for row in rows:
            url = str(row.get("url") or "")
            if not url:
                continue
            source = row.get("source") or {}
            snippet = str(row.get("snippet") or "")
            results.append(SearchResult(
                url=url,
                title=str(row.get("title") or ""),
                snippet=snippet,
                content=snippet,
                date=str(row.get("date") or ""),
                site=str(source.get("name") or source.get("domain") or ""),
                source=self.name,
                raw={
                    "source": source,
                    "search_type": self.search_type,
                    "region": self.region,
                },
            ))
        return results
