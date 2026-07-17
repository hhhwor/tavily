"""Web ranking policy."""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from src.domain.search import SearchResult
from src.pipeline.dedup import normalize_url
from src.pipeline.fusion import rrf_prepare
from src.pipeline.ranking_options import parse_ranking_profile, parse_threshold_mode
from src.ranking.core import (
    PRIOR,
    TEXT,
    DomainConfig,
    RerankContext,
    build_rerank_context,
    normalized_feature,
    rerank_domain,
)
from src.ranking.ports import Reranker


def _key(result: SearchResult, index: int) -> str:
    return normalize_url(result.url) or result.url or f"web:{index}"


def _compress(result: SearchResult, max_chars: int = 320) -> str:
    parts: List[str] = []
    title = (result.title or "").strip()
    snippet = (result.snippet or "").strip()
    content = (result.content or "").strip()
    if title:
        parts.append(title)
    if snippet:
        parts.append(snippet)
    prefix = content[: max(len(snippet) + 80, 120)] if snippet else content[:120]
    if content and (not snippet or snippet not in prefix):
        parts.append(content)
    return "\n".join(parts).strip()[:max_chars]


def _prior(
    result: SearchResult,
    index: int,
    pool: Sequence[SearchResult],
    ctx: RerankContext,
) -> float:
    return normalized_feature(
        pool,
        index,
        lambda candidate: float(candidate.raw.get("_rrf_prior", 0.0)),
    )


def _tie(result: SearchResult, index: int) -> Tuple[Any, ...]:
    return (
        result.provider_rank if result.provider_rank is not None else 10**9,
        result.url,
    )


def build_web_config(
    threshold: float = 0.3,
    max_chars: int = 320,
    text_weight: float = 0.85,
    rrf_weight: float = 0.15,
    pass_bonus: float = 0.02,
    profile: str = "quality",
    threshold_mode: str = "prefer",
) -> DomainConfig[SearchResult]:
    effective_profile = parse_ranking_profile(profile)

    def weights(query: str, ctx: RerankContext) -> Dict[str, float]:
        if effective_profile == "semantic":
            return {TEXT: 1.0, PRIOR: 0.0}
        if effective_profile == "fast":
            return {TEXT: 0.0, PRIOR: 1.0}
        return {TEXT: text_weight, PRIOR: rrf_weight}

    def tiebreaker(result: SearchResult, index: int) -> Tuple[Any, ...]:
        if effective_profile == "semantic":
            return (_key(result, index), index)
        return _tie(result, index)

    return DomainConfig(
        name="web",
        key_fn=_key,
        compress_fn=lambda result: _compress(result, max_chars=max_chars),
        feature_fns={PRIOR: _prior},
        weight_fn=weights,
        profile=effective_profile,
        threshold=threshold,
        threshold_mode=parse_threshold_mode(threshold_mode),
        score_text=effective_profile != "fast",
        prepare_fn=rrf_prepare,
        pass_bonus=pass_bonus,
        tiebreaker_fn=tiebreaker,
    )


class WebReranker(Reranker):
    def __init__(
        self,
        inner: Reranker,
        max_chars: int = 320,
        text_weight: float = 0.85,
        rrf_weight: float = 0.15,
        pass_bonus: float = 0.02,
        threshold: float = 0.3,
        profile: str = "quality",
        threshold_mode: str = "prefer",
    ) -> None:
        self._inner = inner
        self._config = build_web_config(
            threshold=threshold,
            max_chars=max_chars,
            text_weight=text_weight,
            rrf_weight=rrf_weight,
            pass_bonus=pass_bonus,
            profile=profile,
            threshold_mode=threshold_mode,
        )
        self.name = f"web:{inner.name}"
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
        return rerank_domain(query, results, self._config, self._inner, ctx, top_k)

    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        return self.rerank_with_context(
            query, results, top_k, build_rerank_context(query)
        )
