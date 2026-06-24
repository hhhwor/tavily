"""学术论文检索 Provider —— OpenAlex 数据(经 Chukonu 检索系统的 ES 服务)。

数据源切换(2026-06-21):由直连公网 `api.openalex.org` 改为本地 **Chukonu 检索系统**
(`http://localhost:9001`)的 `/openalex/search/keyword` 端点——其 ES 已灌入 5 万条真实
OpenAlex 论文,摘要已重建、字段已结构化,无需公网 key、无速率限制。

  - 端点:   POST {base}/openalex/search/keyword   body {query, size, year_min/max?}
  - 鉴权:   可选 X-API-Key(服务未配 SE4AI_API_KEYS 时全部放行,本机即此状态)
  - 文档:   /home/ec2-user/chuonu-search-solution/patent_search_engine/API.md

实现 SearchProvider 接口,返回 AcademicResult(SearchResult 子类),
故可直接复用现有 cross-encoder reranker 对「标题+摘要」打分。
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional

import requests

from src.models import AcademicResult
from src.providers.base import SearchProvider

_DEFAULT_BASE = "http://localhost:9001"
_SEARCH_PATH = "/openalex/search/keyword"


class OpenAlexProvider(SearchProvider):
    # 数据源标识:openalex_local —— 本地 Chukonu 服务的 OpenAlex 子集,
    # 与历史的公网 OpenAlex API 源(原 "openalex")区分
    name = "openalex_local"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        per_page: int = 25,
        timeout: int = 15,
    ):
        self.base_url = (
            base_url if base_url is not None else os.getenv("OPENALEX_API_URL", _DEFAULT_BASE)
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("OPENALEX_API_KEY", "")
        self.per_page = max(1, min(per_page, 100))  # 服务 size 上限 100
        self.timeout = timeout

    def search(
        self, query: str, top_k: int = 10, recency: Optional[str] = None
    ) -> List[AcademicResult]:
        query = (query or "").strip()
        if not query or not self.base_url:
            return []

        size = min(top_k or self.per_page, self.per_page)
        body: Dict[str, Any] = {"query": query, "size": size}
        # 时效:keyword 端点只支持 year_min/year_max(需成对);把任意 recency 近似为「今年」
        if recency in ("day", "week", "month", "year"):
            yr = date.today().year
            body["year_min"] = yr
            body["year_max"] = yr

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        try:
            resp = requests.post(
                f"{self.base_url}{_SEARCH_PATH}",
                json=body, headers=headers, timeout=self.timeout,
            )
            resp.raise_for_status()
            hits = resp.json().get("results", [])
        except Exception as e:
            print(f"[openalex] 检索失败: {e}")
            return []

        return self._normalize(hits)[:size]

    def _normalize(self, hits: List[Dict[str, Any]]) -> List[AcademicResult]:
        results: List[AcademicResult] = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            try:
                results.append(self._to_result(h))
            except Exception as e:  # 单条异常不影响整体
                print(f"[openalex] 跳过一条解析失败的结果: {e}")
        return results

    def _to_result(self, h: Dict[str, Any]) -> AcademicResult:
        abstract = h.get("abstract", "") or ""
        doi = (h.get("doi", "") or "").strip()
        work_id = h.get("work_id", "") or ""
        is_oa = bool(h.get("is_oa"))

        # 无原生落地页:DOI 优先,退化到 OpenAlex 作品页
        if doi:
            url = f"https://doi.org/{doi}"
        elif work_id:
            url = f"https://openalex.org/{work_id}"
        else:
            url = ""
        oa_url = url if (is_oa and (doi or work_id)) else ""

        venue = h.get("venue", "") or ""
        # authors 是空格拼接的整串(无单作者分隔符),整体存为一项;无则用 first_author
        authors_str = (h.get("authors") or "").strip() or (h.get("first_author") or "").strip()
        authors = [authors_str] if authors_str else []

        return AcademicResult(
            url=url,
            title=h.get("title", "") or "",
            snippet=abstract[:300],
            content=abstract,
            date=h.get("publication_date", "") or "",
            site=venue,
            score=h.get("_score"),
            source=self.name,
            authors=authors,
            year=h.get("publication_year"),
            venue=venue,
            citations=h.get("cited_by_count", 0) or 0,
            doi=doi,
            oa_url=oa_url,
            is_oa=is_oa,
            topic=h.get("primary_topic", "") or "",
            raw=h,
        )
