"""百度千帆 AI 搜索 Provider (web_search)。

接口文档: https://cloud.baidu.com/doc/qianfan-api/s/em82g4tlk
  - Endpoint: POST https://qianfan.baidubce.com/v2/ai_search/web_search
  - 鉴权:     Authorization: Bearer <QIANFAN_API_KEY>
  - 限制:     查询仅单轮、<=72 字符(汉字算 2 字符),超出截断。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from src.domain.errors import ExternalServiceError
from src.infrastructure.http_errors import external_http_error
from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.domain.search import SearchResult
from src.providers.base import SearchProvider

_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
_QUERY_LIMIT = 72  # 字符配额:汉字算 2

# 超长时优先剥离的口语化前缀(保守:只去明显冗余,不动核心语义)
_FILLER_PREFIXES = (
    "我想知道", "我想了解一下", "我想了解", "我想问一下", "我想问", "请问一下",
    "请问", "帮我查一下", "帮我查", "帮我搜", "帮我", "谁能告诉我", "麻烦", "想了解",
)


def _width(s: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in s)


def trim_query(query: str, limit: int = _QUERY_LIMIT) -> str:
    """按百度规则裁剪到 <=limit 字符配额(汉字算 2)。

    先剥离口语化前缀,再按配额硬截断,尽量保留核心语义。
    """
    q = query.strip()
    if _width(q) <= limit:
        return q
    for f in _FILLER_PREFIXES:
        if q.startswith(f):
            q = q[len(f):].strip()
            break
    if _width(q) <= limit:
        return q
    out: List[str] = []
    used = 0
    for ch in q:
        w = 2 if ord(ch) > 127 else 1
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out)


class BaiduSearchProvider(SearchProvider):
    name = "baidu"
    descriptor = SourceDescriptor(
        id=name,
        kind="web",
        capabilities=frozenset({"recency_filter", "full_content"}),
        data_license="baidu-qianfan-terms",
        default_language="zh",
        jurisdictions=("CN",),
        max_candidates=50,
        count_empty_as_used=True,
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 15,
        http_session: Optional[requests.Session] = None,
    ):
        self.api_key = api_key or ""
        self.timeout = timeout
        self._http = http_session or requests
        if not self.api_key:
            raise ValueError("缺少百度千帆凭证: QIANFAN_API_KEY")

    def actual_query(self, request: RetrievalRequest) -> str:
        return trim_query(request.query)

    def actual_filters(self, request: RetrievalRequest) -> Dict[str, Any]:
        filters: Dict[str, Any] = {
            "resource_type": "web",
            "top_k": min(request.candidate_budget, 50),
        }
        if request.recency:
            filters["search_recency_filter"] = {
                "day": "week",
                "week": "week",
                "month": "month",
                "year": "year",
            }.get(request.recency, "month")
        return filters

    def search(self, query: str, top_k: int = 10, recency: Optional[str] = None) -> List[SearchResult]:
        body: Dict[str, Any] = {
            "messages": [{"role": "user", "content": trim_query(query)}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": min(max(top_k, 1), 50)}],
        }
        # 时效过滤:recency bucket → search_recency_filter(枚举 week/month/year)
        if recency:
            body["search_recency_filter"] = {"day": "week", "week": "week",
                                             "month": "month", "year": "year"}.get(recency, "month")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._http.post(_ENDPOINT, headers=headers, json=body, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if "references" not in data:
                cause = RuntimeError(str(data.get("message") or data.get("msg") or data))
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
        return self._normalize(data["references"])[:top_k]

    def _normalize(self, refs: List[Dict[str, Any]]) -> List[SearchResult]:
        results: List[SearchResult] = []
        for r in refs:
            body = r.get("content", "") or ""
            results.append(
                SearchResult(
                    url=r.get("url", ""),
                    title=r.get("title", ""),
                    snippet=r.get("snippet", "") or body[:200],
                    content=body,
                    date=r.get("date", "") or "",
                    site=r.get("website", "") or "",
                    score=None,
                    source=self.name,
                    raw=r,
                )
            )
        return results
