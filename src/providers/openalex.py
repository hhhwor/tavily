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

from dataclasses import replace
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from src.infrastructure.http_errors import external_http_error
from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.models import AcademicResult
from src.providers.base import SearchProvider

_DEFAULT_BASE = "http://localhost:9001"
_SEARCH_PATH = "/openalex/search/keyword"


def _first_str(*values: Any) -> str:
    """返回第一个非空字符串。"""
    for value in values:
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value
    return ""


def _looks_like_pdf_url(url: str) -> bool:
    """基于轻量规则判断是否更像 PDF 直链而非落地页。"""
    if not url:
        return False
    lower = url.lower()
    path = urlparse(url).path.lower()
    return (
        path.endswith(".pdf")
        or "/pdf" in path
        or "downloadpdf" in lower
        or "full.pdf" in lower
    )


def _canonical_work_url(doi: str, work_id: str) -> str:
    """论文主页面: DOI 优先,退化到 OpenAlex 作品页。"""
    if doi:
        return f"https://doi.org/{doi}"
    if work_id:
        return f"https://openalex.org/{work_id}"
    return ""


def _extract_oa_links(h: Dict[str, Any], canonical_url: str, is_oa: bool) -> tuple[str, str, str]:
    """提取 OA 落地页 / PDF 直链。

    兼容三类来源:
    1. Chukonu/OpenAlex 未来显式透出的扁平字段 `oa_landing_url` / `oa_pdf_url`
    2. OpenAlex 原生嵌套字段 `best_oa_location` / `primary_location` / `content_urls`
    3. 仅有 `open_access.oa_url` 时,按 URL 形态区分 landing vs pdf

    当前 Chukonu `OpenAlexHit` 尚未把 dedicated OA URL 字段透出;这种情况下若 `is_oa=true`,
    `oa_landing_url` 回退到 canonical DOI/OpenAlex 页面,而 `oa_pdf_url` 维持空字符串。
    """
    best = h.get("best_oa_location") if isinstance(h.get("best_oa_location"), dict) else {}
    primary = h.get("primary_location") if isinstance(h.get("primary_location"), dict) else {}
    open_access = h.get("open_access") if isinstance(h.get("open_access"), dict) else {}
    content_urls = h.get("content_urls") if isinstance(h.get("content_urls"), dict) else {}

    oa_landing_url = _first_str(
        h.get("oa_landing_url"),
        best.get("landing_page_url"),
        primary.get("landing_page_url"),
    )
    oa_pdf_url = _first_str(
        h.get("oa_pdf_url"),
        content_urls.get("pdf"),
        best.get("pdf_url"),
        primary.get("pdf_url"),
    )

    generic_oa_url = _first_str(
        open_access.get("oa_url"),
        h.get("oa_url"),
    )
    if generic_oa_url:
        if not oa_pdf_url and _looks_like_pdf_url(generic_oa_url):
            oa_pdf_url = generic_oa_url
        if not oa_landing_url and not _looks_like_pdf_url(generic_oa_url):
            oa_landing_url = generic_oa_url

    if is_oa and not oa_landing_url:
        oa_landing_url = canonical_url

    oa_url = _first_str(oa_landing_url, oa_pdf_url)
    return oa_url, oa_landing_url, oa_pdf_url


class OpenAlexProvider(SearchProvider):
    # 数据源标识:openalex_local —— 本地 Chukonu 服务的 OpenAlex 子集,
    # 与历史的公网 OpenAlex API 源(原 "openalex")区分
    name = "openalex_local"
    descriptor = SourceDescriptor(
        id=name,
        kind="academic",
        capabilities=frozenset({"recency_filter", "time_range_filter", "open_access_metadata"}),
        snapshot_capability="service_index",
        default_snapshot="service-index:unspecified",
        data_license="OpenAlex",
        max_candidates=100,
    )

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        per_page: int = 25,
        timeout: int = 15,
        http_session: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url if base_url is not None else _DEFAULT_BASE).rstrip("/")
        self.api_key = api_key or ""
        self.per_page = max(1, min(per_page, 100))  # 服务 size 上限 100
        self.timeout = timeout
        self._http = http_session or requests
        self.descriptor = replace(self.descriptor, max_candidates=self.per_page)

    def actual_filters(self, request: RetrievalRequest) -> Dict[str, Any]:
        filters: Dict[str, Any] = {"size": min(request.candidate_budget, self.per_page)}
        if request.recency in ("day", "week", "month", "year"):
            year = (request.time_to or date.today()).year
            filters.update({"year_min": year, "year_max": year})
        return filters

    def search(
        self, query: str, top_k: int = 10, recency: Optional[str] = None
    ) -> List[AcademicResult]:
        return self._search(query, top_k, recency, request=None)

    def search_request(self, request: RetrievalRequest) -> List[AcademicResult]:
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
    ) -> List[AcademicResult]:
        query = (query or "").strip()
        if not query or not self.base_url:
            return []

        size = min(top_k or self.per_page, self.per_page)
        body: Dict[str, Any] = {"query": query, "size": size}
        # 时效:keyword 端点只支持 year_min/year_max(需成对);把任意 recency 近似为「今年」
        if recency in ("day", "week", "month", "year"):
            yr = (request.time_to if request and request.time_to else date.today()).year
            body["year_min"] = yr
            body["year_max"] = yr

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        try:
            resp = self._http.post(
                f"{self.base_url}{_SEARCH_PATH}",
                json=body, headers=headers, timeout=self.timeout,
            )
            resp.raise_for_status()
            hits = resp.json().get("results", [])
        except Exception as exc:
            raise external_http_error(self.name, "search", exc) from exc

        return self._normalize(hits)[:size]

    def _normalize(self, hits: List[Dict[str, Any]]) -> List[AcademicResult]:
        results: List[AcademicResult] = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            try:
                results.append(self._to_result(h))
            except Exception:  # 单条异常不影响整体，也不记录不可信 payload
                continue
        return results

    def _to_result(self, h: Dict[str, Any]) -> AcademicResult:
        abstract = h.get("abstract", "") or ""
        doi = (h.get("doi", "") or "").strip()
        work_id = h.get("work_id", "") or ""
        is_oa = bool(h.get("is_oa"))

        # 论文主页面固定为 DOI/OpenAlex 页面; OA 页面/PDF 直链走单独字段。
        url = _canonical_work_url(doi, work_id)
        oa_url, oa_landing_url, oa_pdf_url = _extract_oa_links(h, url, is_oa)

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
            work_id=work_id,
            year=h.get("publication_year"),
            venue=venue,
            citations=h.get("cited_by_count", 0) or 0,
            doi=doi,
            oa_url=oa_url,
            oa_landing_url=oa_landing_url,
            oa_pdf_url=oa_pdf_url,
            license=(h.get("license", "") or ""),
            license_id=(h.get("license_id", "") or ""),
            is_oa=is_oa,
            oa_status=(h.get("oa_status", "") or ""),
            topic=h.get("primary_topic", "") or "",
            raw=h,
        )
