"""Academic ranking policy."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence, Tuple

from src.models import AcademicResult, SearchResult
from src.pipeline.ranking_options import parse_ranking_profile, parse_threshold_mode
from src.ranking.core import (
    CITATIONS,
    FRESHNESS,
    OA,
    SOURCE_SCORE,
    TEXT,
    VENUE,
    DomainConfig,
    RerankContext,
    build_rerank_context,
    normalized_feature,
    parse_date_days_ago,
    rerank_domain,
)
from src.ranking.ports import Reranker


def _key(result: AcademicResult, index: int) -> str:
    return result.doi or result.url or f"{result.title}|{result.year}|{index}"


def _compress(result: AcademicResult, max_chars: int = 480) -> str:
    title = (result.title or "").strip()
    body = (result.content or result.snippet or "").strip()
    if not title:
        return body[:max_chars]
    available = max(0, max_chars - len(title) - 1)
    return f"{title}\n{body[:available]}".strip()


def _citations(
    result: AcademicResult,
    index: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    return normalized_feature(
        pool, index, lambda paper: math.log1p(max(0, paper.citations))
    )


def _source_score(
    result: AcademicResult,
    index: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    return normalized_feature(pool, index, lambda paper: paper.score)


def _freshness(
    result: AcademicResult,
    index: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    days_ago = parse_date_days_ago(result.date, ctx.reference_time)
    if days_ago is None and result.year:
        try:
            date = datetime(int(result.year), 1, 1, tzinfo=timezone.utc)
            now = ctx.reference_time or datetime.now(timezone.utc)
            days_ago = max(0, (now - date).days)
        except (TypeError, ValueError):
            days_ago = None
    if days_ago is None:
        return 0.4 if ctx.wants_recent else 0.5
    if ctx.wants_recent:
        return 1.0 / (1.0 + days_ago / 365.0)
    return max(0.0, 1.0 - days_ago / (365.0 * 20.0))


def _venue(
    result: AcademicResult,
    index: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    venue = result.venue or result.site
    if not venue:
        return 0.0
    lower = venue.lower()
    if any(source in lower for source in ("arxiv", "biorxiv", "medrxiv", "ssrn")):
        return 0.35
    return 1.0


def _open_access(
    result: AcademicResult,
    index: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    if result.oa_pdf_url:
        return 1.0
    if result.oa_landing_url or result.is_oa:
        return 0.7
    return 0.0


def _quality_weights(ctx: RerankContext) -> Dict[str, float]:
    if ctx.wants_recent and ctx.wants_foundational:
        return {TEXT: 0.66, CITATIONS: 0.16, FRESHNESS: 0.12, VENUE: 0.04, OA: 0.02}
    if ctx.wants_recent:
        return {TEXT: 0.68, CITATIONS: 0.08, FRESHNESS: 0.18, VENUE: 0.04, OA: 0.02}
    if ctx.wants_foundational:
        return {TEXT: 0.66, CITATIONS: 0.26, FRESHNESS: 0.01, VENUE: 0.05, OA: 0.02}
    return {TEXT: 0.70, CITATIONS: 0.20, FRESHNESS: 0.02, VENUE: 0.05, OA: 0.03}


def _tie(result: AcademicResult, index: int) -> Tuple[Any, ...]:
    return (
        -(float(result.score) if result.score is not None else 0.0),
        -max(0, result.citations),
        -(result.year or 0),
        result.title,
        index,
    )


def build_academic_config(
    threshold: float = 0.3,
    max_docs: int = 25,
    max_chars: int = 480,
    profile: str = "quality",
    threshold_mode: str = "prefer",
) -> DomainConfig[AcademicResult]:
    effective_profile = parse_ranking_profile(profile)

    def weights(query: str, ctx: RerankContext) -> Dict[str, float]:
        if effective_profile == "semantic":
            return {
                TEXT: 1.0,
                SOURCE_SCORE: 0.0,
                CITATIONS: 0.0,
                FRESHNESS: 0.0,
                VENUE: 0.0,
                OA: 0.0,
            }
        if effective_profile == "fast":
            return {
                TEXT: 0.0,
                SOURCE_SCORE: 1.0,
                CITATIONS: 0.0,
                FRESHNESS: 0.0,
                VENUE: 0.0,
                OA: 0.0,
            }
        return {SOURCE_SCORE: 0.0, **_quality_weights(ctx)}

    def tiebreaker(result: AcademicResult, index: int) -> Tuple[Any, ...]:
        if effective_profile == "semantic":
            return (_key(result, index), index)
        if effective_profile == "fast":
            return (index,)
        return _tie(result, index)

    return DomainConfig(
        name="academic",
        key_fn=_key,
        compress_fn=lambda paper: _compress(paper, max_chars=max_chars),
        feature_fns={
            SOURCE_SCORE: _source_score,
            CITATIONS: _citations,
            FRESHNESS: _freshness,
            VENUE: _venue,
            OA: _open_access,
        },
        weight_fn=weights,
        profile=effective_profile,
        threshold=threshold,
        threshold_mode=parse_threshold_mode(threshold_mode),
        score_text=effective_profile != "fast",
        max_docs=max_docs,
        tiebreaker_fn=tiebreaker,
    )


class AcademicReranker(Reranker):
    def __init__(
        self,
        inner: Reranker,
        max_docs: int = 25,
        max_chars: int = 480,
        threshold: float = 0.3,
        profile: str = "quality",
        threshold_mode: str = "prefer",
    ) -> None:
        self._inner = inner
        self._config = build_academic_config(
            threshold=threshold,
            max_docs=max_docs,
            max_chars=max_chars,
            profile=profile,
            threshold_mode=threshold_mode,
        )
        self.name = f"academic:{inner.name}"
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
        if not all(isinstance(result, AcademicResult) for result in results):
            return self._inner.rerank(query, results, top_k)
        papers = [result for result in results if isinstance(result, AcademicResult)]
        return rerank_domain(query, papers, self._config, self._inner, ctx, top_k)

    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        return self.rerank_with_context(
            query, results, top_k, build_rerank_context(query)
        )
