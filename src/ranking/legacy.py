"""Deprecated wrappers retained for evaluation scripts during migration."""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from src.domain.search import SearchResult
from src.ranking.core import parse_date_days_ago
from src.ranking.factory import build_text_scorer
from src.ranking.ports import NoOpReranker, Reranker

_DEFAULT_AUTHORITY = {"serpapi": 0.95, "tencent": 0.90, "baidu": 0.85}


class ThresholdReranker(Reranker):
    def __init__(self, inner: Reranker, threshold: float = 0.3) -> None:
        self._inner = inner
        self._threshold = threshold
        self.name = inner.name
        self.supports_text_scoring = inner.supports_text_scoring

    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        ranked = self._inner.rerank(query, results, top_k)
        if self._threshold <= 0:
            return ranked
        return [
            result
            for result in ranked
            if (result.rerank_score or 0.0) >= self._threshold
        ][:top_k]

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return self._inner.score(query, texts)


class FusionReranker(Reranker):
    def __init__(
        self,
        inner: Reranker,
        time_sensitive: bool = False,
        alpha: float = 0.7,
        beta: float = 0.15,
        gamma: float = 0.10,
        delta: float = 0.05,
        authority_weights: Optional[dict] = None,
    ) -> None:
        self._inner = inner
        self._time_sensitive = time_sensitive
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._delta = delta
        self._authority = authority_weights or _DEFAULT_AUTHORITY
        self.name = inner.name
        self.supports_text_scoring = inner.supports_text_scoring

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return self._inner.score(query, texts)

    def _freshness_score(self, days_ago: Optional[int]) -> float:
        if days_ago is None:
            return 0.5
        if self._time_sensitive:
            return 1.0 / (1.0 + days_ago)
        return max(0.0, 1.0 - days_ago / 365.0)

    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        ranked = [
            result.model_copy(deep=True)
            for result in self._inner.rerank(query, results, top_k)
        ]
        for result in ranked:
            text = result.rerank_score or 0.0
            result.rerank_score = (
                self._alpha * text
                + self._beta * self._freshness_score(parse_date_days_ago(result.date))
                + self._gamma * self._authority.get(result.source, 0.8)
                + self._delta * (1.0 / (1.0 + (result.provider_rank or 0)))
            )
        return sorted(
            ranked, key=lambda result: result.rerank_score or 0.0, reverse=True
        )[:top_k]


def build_reranker(
    enabled: bool,
    backend: str,
    model_name: str,
    cache_dir: str,
    device: Optional[str] = None,
    chunk_max_chars: int = 400,
    chunk_overlap: int = 50,
    threshold: float = 0.3,
    siliconflow_api_key: str = "",
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1",
    http_session: Any = None,
    fusion_enabled: bool = True,
    fusion_time_sensitive: bool = False,
    fusion_alpha: float = 0.7,
    fusion_beta: float = 0.15,
    fusion_gamma: float = 0.10,
    fusion_delta: float = 0.05,
) -> Reranker:
    inner = build_text_scorer(
        enabled=enabled,
        backend=backend,
        model_name=model_name,
        cache_dir=cache_dir,
        device=device,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        siliconflow_api_key=siliconflow_api_key,
        siliconflow_base_url=siliconflow_base_url,
        http_session=http_session,
    )
    if isinstance(inner, NoOpReranker):
        return inner
    reranker: Reranker = ThresholdReranker(inner, threshold=threshold)
    if fusion_enabled:
        reranker = FusionReranker(
            reranker,
            time_sensitive=fusion_time_sensitive,
            alpha=fusion_alpha,
            beta=fusion_beta,
            gamma=fusion_gamma,
            delta=fusion_delta,
        )
    return reranker
