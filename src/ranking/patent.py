"""Patent ranking policy."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

from src.domain.search import PatentResult, SearchResult
from src.pipeline.ranking_options import parse_ranking_profile, parse_threshold_mode
from src.ranking.core import (
    CITATIONS,
    FRESHNESS,
    SOURCE_SCORE,
    STATUS,
    TEXT,
    DomainConfig,
    RerankContext,
    build_rerank_context,
    normalized_feature,
    parse_date_days_ago,
    rerank_domain,
)
from src.ranking.ports import Reranker


def _key(result: PatentResult, index: int) -> str:
    return (
        result.publication_number
        or result.url
        or f"{result.title}|{result.application_number}|{index}"
    )


def _compress(result: PatentResult, max_chars: int = 520) -> str:
    parts: List[str] = []
    if result.title:
        parts.append(result.title.strip())
    body = (result.content or result.snippet or "").strip()
    if body:
        parts.append(f"摘要: {body}")
    applicants = [applicant for applicant in result.applicant[:3] if applicant]
    if applicants:
        parts.append(f"申请人: {'; '.join(applicants)}")
    classification = result.ipc_main or result.cpc_main
    if classification:
        parts.append(f"分类: {classification}")
    if result.publication_number:
        parts.append(f"公开号: {result.publication_number}")
    return "\n".join(parts).strip()[:max_chars]


def _source_score(
    result: PatentResult,
    index: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    return normalized_feature(pool, index, lambda patent: patent.score)


def _freshness(
    result: PatentResult,
    index: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    days_ago = parse_date_days_ago(
        result.publication_date or result.application_date or result.date,
        ctx.reference_time,
    )
    if days_ago is None:
        return 0.4 if ctx.wants_recent else 0.5
    if ctx.wants_recent or ctx.time_sensitive:
        return 1.0 / (1.0 + days_ago / 365.0)
    return max(0.0, 1.0 - days_ago / (365.0 * 10.0))


def _citations(
    result: PatentResult,
    index: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    return normalized_feature(
        pool, index, lambda patent: math.log1p(max(0, patent.citation_count))
    )


def _status(
    result: PatentResult,
    index: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    if not result.status:
        return 0.5
    lower = result.status.lower()
    if any(value in lower for value in ("active", "granted", "grant", "pending", "published", "alive")):
        return 1.0
    if any(value in lower for value in ("expired", "withdrawn", "abandoned", "lapsed", "dead")):
        return 0.2
    return 0.5


def _quality_weights(ctx: RerankContext) -> Dict[str, float]:
    if ctx.wants_recent or ctx.time_sensitive:
        return {TEXT: 0.70, SOURCE_SCORE: 0.10, FRESHNESS: 0.12, CITATIONS: 0.04, STATUS: 0.04}
    return {TEXT: 0.72, SOURCE_SCORE: 0.12, FRESHNESS: 0.06, CITATIONS: 0.06, STATUS: 0.04}


def _tie(result: PatentResult, index: int) -> Tuple[Any, ...]:
    return (
        -(float(result.score) if result.score is not None else 0.0),
        -max(0, result.citation_count),
        result.publication_number,
        result.title,
        index,
    )


def build_patent_config(
    threshold: float = 0.3,
    max_chars: int = 520,
    profile: str = "quality",
    threshold_mode: str = "prefer",
) -> DomainConfig[PatentResult]:
    effective_profile = parse_ranking_profile(profile)

    def weights(query: str, ctx: RerankContext) -> Dict[str, float]:
        if effective_profile == "semantic":
            return {TEXT: 1.0, SOURCE_SCORE: 0.0, FRESHNESS: 0.0, CITATIONS: 0.0, STATUS: 0.0}
        if effective_profile == "fast":
            return {TEXT: 0.0, SOURCE_SCORE: 1.0, FRESHNESS: 0.0, CITATIONS: 0.0, STATUS: 0.0}
        return _quality_weights(ctx)

    def tiebreaker(result: PatentResult, index: int) -> Tuple[Any, ...]:
        if effective_profile == "semantic":
            return (_key(result, index), index)
        if effective_profile == "fast":
            return (index,)
        return _tie(result, index)

    return DomainConfig(
        name="patent",
        key_fn=_key,
        compress_fn=lambda patent: _compress(patent, max_chars=max_chars),
        feature_fns={
            SOURCE_SCORE: _source_score,
            FRESHNESS: _freshness,
            CITATIONS: _citations,
            STATUS: _status,
        },
        weight_fn=weights,
        profile=effective_profile,
        threshold=threshold,
        threshold_mode=parse_threshold_mode(threshold_mode),
        score_text=effective_profile != "fast",
        tiebreaker_fn=tiebreaker,
    )


class PatentReranker(Reranker):
    def __init__(
        self,
        inner: Reranker,
        max_chars: int = 520,
        threshold: float = 0.3,
        profile: str = "quality",
        threshold_mode: str = "prefer",
    ) -> None:
        self._inner = inner
        self._config = build_patent_config(
            threshold=threshold,
            max_chars=max_chars,
            profile=profile,
            threshold_mode=threshold_mode,
        )
        self.name = f"patent:{inner.name}"
        self.supports_text_scoring = inner.supports_text_scoring

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return self._inner.score(query, texts)

    def rerank_with_context(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int,
        ctx: RerankContext,
    ) -> List[SearchResult]:
        if not results:
            return []
        if not all(isinstance(result, PatentResult) for result in results):
            return self._inner.rerank(query, results, top_k)
        patents = [result for result in results if isinstance(result, PatentResult)]
        return rerank_domain(query, patents, self._config, self._inner, ctx, top_k)

    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        return self.rerank_with_context(
            query, results, top_k, build_rerank_context(query)
        )
