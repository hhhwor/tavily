"""Stable ranking interfaces shared by policies and scorer adapters."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from src.models import SearchResult


class Reranker(ABC):
    name: str = "base"
    supports_text_scoring: bool = True

    @abstractmethod
    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        raise NotImplementedError

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        pseudo = [
            SearchResult(url=f"__text_{i}", title="", content=text or "")
            for i, text in enumerate(texts)
        ]
        self.rerank(query, pseudo, len(pseudo))
        return [clamp01(result.rerank_score) for result in pseudo]


class NoOpReranker(Reranker):
    name = "noop"
    supports_text_scoring = False

    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        ordered = sorted(
            results,
            key=lambda result: (
                result.score is not None,
                result.score or 0.0,
            ),
            reverse=True,
        )
        return ordered[:top_k]

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return [0.0 for _ in texts]


def sigmoid_normalize(scores: List[float], temperature: float = 1.0) -> List[float]:
    return [1.0 / (1.0 + math.exp(-score * temperature)) for score in scores]


def clamp01(value: Optional[float], default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return max(0.0, min(1.0, number))
