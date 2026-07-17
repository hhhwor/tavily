"""腾讯云联网搜索 API (SearchPro) Provider。

接口文档: https://cloud.tencent.com/document/product/1806/121811
  - Action: SearchPro / Version: 2025-05-08 / Endpoint: wsa.tencentcloudapi.com
  - 鉴权:   TC3-HMAC-SHA256 (SecretId + SecretKey),纯标准库实现。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from src.domain.errors import ExternalServiceError
from src.infrastructure.http_errors import external_http_error
from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.domain.search import SearchResult
from src.providers.base import SearchProvider

_SERVICE = "wsa"
_HOST = "wsa.tencentcloudapi.com"
_ENDPOINT = "https://wsa.tencentcloudapi.com"
_ACTION = "SearchPro"
_VERSION = "2025-05-08"
_ALGORITHM = "TC3-HMAC-SHA256"


def _sign_v3(secret_id: str, secret_key: str, payload: str) -> Dict[str, str]:
    """计算 TC3-HMAC-SHA256 鉴权头。"""
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    ct = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{ct}\nhost:{_HOST}\nx-tc-action:{_ACTION.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
    )

    credential_scope = f"{date}/{_SERVICE}/tc3_request"
    hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "\n".join([_ALGORITHM, str(timestamp), credential_scope, hashed_canonical])

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _h(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _h(secret_date, _SERVICE)
    secret_signing = _h(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{_ALGORITHM} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Content-Type": ct,
        "Host": _HOST,
        "X-TC-Action": _ACTION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": _VERSION,
    }


class TencentSearchProvider(SearchProvider):
    name = "tencent"
    descriptor = SourceDescriptor(
        id=name,
        kind="web",
        capabilities=frozenset({"recency_filter", "time_range_filter", "full_content"}),
        data_license="tencent-cloud-terms",
        default_language="zh",
        jurisdictions=("CN",),
        count_empty_as_used=True,
    )

    def __init__(
        self,
        secret_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        timeout: int = 15,
        http_session: Optional[requests.Session] = None,
    ):
        self.secret_id = secret_id or ""
        self.secret_key = secret_key or ""
        self.timeout = timeout
        self._http = http_session or requests
        if not self.secret_id or not self.secret_key:
            raise ValueError("缺少腾讯云凭证: TENCENT_SECRET_ID / TENCENT_SECRET_KEY")

    def actual_filters(self, request: RetrievalRequest) -> Dict[str, Any]:
        filters: Dict[str, Any] = {}
        if request.recency and request.time_from and request.time_to:
            filters["FromTime"] = int(request.time_from.timestamp())
            filters["ToTime"] = int(request.time_to.timestamp())
        return filters

    def search(self, query: str, top_k: int = 10, recency: Optional[str] = None) -> List[SearchResult]:
        return self._search(query, top_k, recency, request=None)

    def search_request(self, request: RetrievalRequest) -> List[SearchResult]:
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
        recency: Optional[str],
        *,
        request: Optional[RetrievalRequest],
    ) -> List[SearchResult]:
        body: Dict[str, Any] = {"Query": query, "Mode": 0}
        # 时效过滤:recency bucket → FromTime/ToTime(Unix 时间戳)
        if request is not None:
            body.update(self.actual_filters(request))
        elif recency:
            delta = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400,
                     "year": 365 * 86400}.get(recency)
            if delta:
                now = int(time.time())
                body["FromTime"] = now - delta
                body["ToTime"] = now
        payload = json.dumps(body, ensure_ascii=False)
        headers = _sign_v3(self.secret_id, self.secret_key, payload)

        try:
            resp = self._http.post(
                _ENDPOINT, headers=headers, data=payload.encode("utf-8"), timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json().get("Response", {})
            if "Error" in data:
                cause = RuntimeError(str(data["Error"]))
                raise ExternalServiceError(
                    provider=self.name,
                    code="SEARCH_UPSTREAM_REJECTED",
                    recoverable=False,
                    cause=cause,
                ) from cause
        except ExternalServiceError:
            raise
        except Exception as exc:
            raise external_http_error(self.name, "search", exc) from exc
        return self._normalize(data.get("Pages", []))[:top_k]

    def _normalize(self, pages: List[str]) -> List[SearchResult]:
        results: List[SearchResult] = []
        for item in pages:
            try:
                p = json.loads(item) if isinstance(item, str) else item
            except (json.JSONDecodeError, TypeError):
                continue
            results.append(
                SearchResult(
                    url=p.get("url", ""),
                    title=p.get("title", ""),
                    snippet=p.get("passage", "") or "",
                    content=p.get("content", "") or "",
                    date=p.get("date", "") or "",
                    site=p.get("site", "") or "",
                    score=p.get("score"),
                    source=self.name,
                    raw=p,
                )
            )
        return results
