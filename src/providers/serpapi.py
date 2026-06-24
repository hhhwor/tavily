"""SerpAPI Google 搜索 Provider。

接口文档: https://serpapi.com/search-api
  - Endpoint: GET https://serpapi.com/search
  - 鉴权:     api_key 查询参数
  - 返回:     organic_results[].{title, link, snippet, position, date}
  - 免费额度: 100 次/月
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from src.models import SearchResult
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

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 15,
        gl: str = "us",
        hl: str = "en",
    ):
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY", "")
        self.timeout = timeout
        self.gl = gl
        self.hl = hl
        if not self.api_key:
            raise ValueError("缺少 SerpAPI 凭证: SERPAPI_API_KEY")

    def search(self, query: str, top_k: int = 10, recency: Optional[str] = None) -> List[SearchResult]:
        params: Dict[str, Any] = {
            "engine": "google",
            "q": query,
            "api_key": self.api_key,
            "num": min(max(top_k, 1), 100),
            "gl": self.gl,
            "hl": self.hl,
        }
        if recency and recency in _RECENCY_TBS:
            params["tbs"] = _RECENCY_TBS[recency]

        resp = requests.get(_ENDPOINT, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"SerpAPI 错误: {data['error']}")

        return self._normalize(data.get("organic_results", []))[:top_k]

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
