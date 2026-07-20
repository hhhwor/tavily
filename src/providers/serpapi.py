"""SerpAPI Google 搜索 Provider。

接口文档: https://serpapi.com/search-api
  - Endpoint: GET https://serpapi.com/search
  - 鉴权:     api_key 查询参数
  - 返回:     organic_results[].{title, link, snippet, position, date}
  - 免费额度: 100 次/月
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from src.domain.errors import ExternalServiceError
from src.infrastructure.http_errors import external_http_error
from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.domain.search import SearchResult
from src.providers.base import SearchProvider

_ENDPOINT = "https://serpapi.com/search"

# recency → Google tbs 参数
_RECENCY_TBS = {
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}


class SerpApiProvider(SearchProvider):
    name = "serpapi"
    descriptor = SourceDescriptor(
        id=name,
        kind="web",
        capabilities=frozenset({"recency_filter", "language_filter", "snippet"}),
        data_license="serpapi-terms",
        max_candidates=100,
        count_empty_as_used=True,
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 15,
        gl: str = "us",
        hl: str = "en",
        http_session: Optional[requests.Session] = None,
    ):
        self.api_key = api_key or ""
        self.timeout = timeout
        self.gl = gl
        self.hl = hl
        self._http = http_session or requests
        if not self.api_key:
            raise ValueError("缺少 SerpAPI 凭证: SERPAPI_API_KEY")

    def actual_filters(self, request: RetrievalRequest) -> Dict[str, Any]:
        language = (
            request.language.lower().replace("_", "-")
            if request.language
            else self.hl
        )
        filters: Dict[str, Any] = {
            "engine": "google",
            "num": min(max(request.candidate_budget, 1), 100),
            "gl": self.gl,
            "hl": language,
        }
        if request.recency in _RECENCY_TBS:
            filters["tbs"] = _RECENCY_TBS[request.recency]
        return filters

    def applied_request_filters(
        self,
        request: RetrievalRequest,
    ) -> Dict[str, Any]:
        # Google hl 只接受一个界面语言，多语言请求不声称已完整应用。
        if len(request.languages) == 1:
            return {"languages": [request.languages[0]]}
        return {}

    def search(self, query: str, top_k: int = 10, recency: Optional[str] = None) -> List[SearchResult]:
        request = RetrievalRequest(
            query=query,
            candidate_budget=top_k,
            recency=recency,
        )
        return self.search_request(request)

    def search_request(self, request: RetrievalRequest) -> List[SearchResult]:
        filters = self.actual_filters(request)
        params: Dict[str, Any] = {
            **filters,
            "q": request.query,
            "api_key": self.api_key,
        }

        try:
            resp = self._http.get(_ENDPOINT, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                cause = RuntimeError(str(data["error"]))
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

        return self._normalize(data.get("organic_results", []))[
            : request.candidate_budget
        ]

    def _normalize(self, items: List[Dict[str, Any]]) -> List[SearchResult]:
        results: List[SearchResult] = []
        for r in items:
            snippet = r.get("snippet", "") or ""
            results.append(
                SearchResult(
                    url=r.get("link", ""),
                    title=r.get("title", ""),
                    snippet=snippet,
                    content=snippet,  # SerpAPI 不返回正文,用 snippet 充当
                    date=r.get("date", "") or "",
                    site=r.get("displayed_link", "") or "",
                    score=None,
                    source=self.name,
                    raw=r,
                )
            )
        return results
