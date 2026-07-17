"""Pure L0 query normalization, intent detection and route planning.

Legacy rewrite functions remain as lazy compatibility shims; production injects a
``QueryRewriter`` into the application planner instead.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Optional

from src.models import SearchFailure, SearchPlan

MAX_QUERY_LEN = 512

_RECENCY_RULES = [
    (re.compile(r"今天|今日|\btoday\b", re.I), "day"),
    (re.compile(r"本周|这周|近.{0,2}周|过去.{0,2}周|最近几天|this week|past week", re.I), "week"),
    (re.compile(r"本月|近.{0,2}个?月|最近.{0,2}个?月|this month|past month", re.I), "month"),
    (re.compile(r"今年|近.{0,2}年|过去.{0,2}年|this year", re.I), "year"),
    (re.compile(r"最新|最近|近期|实时|latest|recent|newest", re.I), "month"),
]
_YEAR = re.compile(r"\b20\d{2}\b")
_ACADEMIC_RULES = re.compile(
    r"论文|文献|综述|预印本|期刊|学术|被引|引文|研究综述|发表|"
    r"\barxiv\b|\bpapers?\b|\bpreprints?\b|\bsurvey\b|\bliterature\b|"
    r"\bcitations?\b|\bcited by\b|\bpeer.?reviewed\b|\bet al\.?|\bdoi\b|"
    r"\bpubmed\b|\bscholar\b|\bjournals?\b|\bproceedings\b|\bbibliograph",
    re.I,
)
_PATENT_RULES = re.compile(
    r"专利|发明专利|实用新型|外观设计|专利申请|专利号|公开号|申请号|授权号|"
    r"权利要求|权要|申请人|发明人|"
    r"\bpatents?\b|\bpatented\b|\bpatentability\b|\binvention\b|\bIPC\b|"
    r"\bUSPTO\b|\bWIPO\b|\bEPO\b",
    re.I,
)


def normalize(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", query or "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip(" ?？。.!！,，、:：")


def detect_recency(query: str) -> Optional[str]:
    for pattern, bucket in _RECENCY_RULES:
        if pattern.search(query):
            return bucket
    return None


def detect_academic(query: str) -> bool:
    return bool(_ACADEMIC_RULES.search(query))


def detect_patent(query: str) -> bool:
    return bool(_PATENT_RULES.search(query))


def rewrite_query(
    query: str,
    api_key: str,
    base_url: str = "https://api.siliconflow.cn/v1",
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    cache_size: int = 512,
    failures: Optional[List[SearchFailure]] = None,
    http_session: Any = None,
) -> str:
    """Deprecated compatibility shim for component evaluation."""
    from src.infrastructure.query_rewriter import rewrite_query as adapter_rewrite

    return adapter_rewrite(
        query,
        api_key,
        base_url,
        model,
        cache_size,
        failures=failures,
        http_session=http_session,
    )


def rewrite_academic_query(
    query: str,
    api_key: str,
    base_url: str = "https://api.siliconflow.cn/v1",
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    cache_size: int = 512,
    failures: Optional[List[SearchFailure]] = None,
    http_session: Any = None,
) -> str:
    """Deprecated compatibility shim for component evaluation."""
    from src.infrastructure.query_rewriter import (
        rewrite_academic_query as adapter_rewrite,
    )

    return adapter_rewrite(
        query,
        api_key,
        base_url,
        model,
        cache_size,
        failures=failures,
        http_session=http_session,
    )


def plan_query(
    query: str,
    providers: List[str],
    top_k: int = 10,
    rewrite: bool = False,
    rewrite_api_key: str = "",
    rewrite_base_url: str = "https://api.siliconflow.cn/v1",
    rewrite_model: str = "Qwen/Qwen2.5-7B-Instruct",
    rewrite_cache_size: int = 512,
    academic_detect: bool = True,
    force_academic: Optional[bool] = None,
    patent_detect: bool = True,
    force_patent: Optional[bool] = None,
    http_session: Any = None,
) -> SearchPlan:
    """Build a route plan; external rewriting is a legacy optional hook."""
    normalized = normalize(query)
    if not normalized:
        raise ValueError("空查询")
    normalized = normalized[:MAX_QUERY_LEN]
    recency = detect_recency(normalized)
    academic = (
        force_academic
        if force_academic is not None
        else academic_detect and detect_academic(normalized)
    )
    patent = (
        force_patent
        if force_patent is not None
        else patent_detect and detect_patent(normalized)
    )
    failures: List[SearchFailure] = []
    rewritten = None
    if rewrite and rewrite_api_key:
        rewritten = rewrite_query(
            normalized,
            rewrite_api_key,
            rewrite_base_url,
            rewrite_model,
            rewrite_cache_size,
            failures=failures,
            http_session=http_session,
        )
    return SearchPlan(
        raw_query=query,
        normalized_query=normalized,
        rewritten_query=rewritten,
        recency=recency,
        time_sensitive=recency is not None or bool(_YEAR.search(normalized)),
        academic=academic,
        patent=patent,
        providers=list(providers),
        top_k=top_k,
        failures=failures,
    )
