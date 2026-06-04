"""腾讯云联网搜索 API (SearchPro) Provider。

接口文档: https://cloud.tencent.com/document/product/1806/121811
  - Action: SearchPro / Version: 2025-05-08 / Endpoint: wsa.tencentcloudapi.com
  - 鉴权:   TC3-HMAC-SHA256 (SecretId + SecretKey),纯标准库实现。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from src.models import SearchResult
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

    def __init__(
        self,
        secret_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        timeout: int = 15,
    ):
        self.secret_id = secret_id or os.getenv("TENCENT_SECRET_ID", "")
        self.secret_key = secret_key or os.getenv("TENCENT_SECRET_KEY", "")
        self.timeout = timeout
        if not self.secret_id or not self.secret_key:
            raise ValueError("缺少腾讯云凭证: TENCENT_SECRET_ID / TENCENT_SECRET_KEY")

    def search(self, query: str, top_k: int = 10, recency: Optional[str] = None) -> List[SearchResult]:
        body: Dict[str, Any] = {"Query": query, "Mode": 0}
        # 时效过滤:recency bucket → FromTime/ToTime(Unix 时间戳)
        if recency:
            delta = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400,
                     "year": 365 * 86400}.get(recency)
            if delta:
                now = int(time.time())
                body["FromTime"] = now - delta
                body["ToTime"] = now
        payload = json.dumps(body, ensure_ascii=False)
        headers = _sign_v3(self.secret_id, self.secret_key, payload)

        resp = requests.post(
            _ENDPOINT, headers=headers, data=payload.encode("utf-8"), timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json().get("Response", {})
        if "Error" in data:
            err = data["Error"]
            raise RuntimeError(
                f"腾讯搜索错误 [{err.get('Code')}] {err.get('Message')} "
                f"(RequestId={data.get('RequestId')})"
            )
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
