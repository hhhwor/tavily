"""FlashRank text-scoring adapter."""
from __future__ import annotations

import threading
from typing import List, Sequence

from src.domain.search import SearchResult
from src.pipeline.chunk import chunk_text
from src.ranking.ports import Reranker, sigmoid_normalize


class FlashRankReranker(Reranker):
    name = "flashrank"

    def __init__(
        self,
        model_name: str,
        cache_dir: str,
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ) -> None:
        from flashrank import Ranker

        self._ranker = Ranker(model_name=model_name, cache_dir=cache_dir)
        self._lock = threading.Lock()
        self.name = f"flashrank:{model_name}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        from flashrank import RerankRequest

        if not texts:
            return []
        pairs = [
            (index, chunk)
            for index, text in enumerate(texts)
            for chunk in chunk_text(
                text or "", self._chunk_max_chars, self._chunk_overlap
            )
        ]
        if not pairs:
            return [0.0 for _ in texts]
        passages = [
            {"id": passage_id, "text": text}
            for passage_id, (_, text) in enumerate(pairs)
        ]
        with self._lock:
            scored = self._ranker.rerank(
                RerankRequest(query=query, passages=passages)
            )
        document_scores: dict[int, float] = {}
        for item in scored:
            document_index = pairs[item["id"]][0]
            score = float(item["score"])
            document_scores[document_index] = max(
                score, document_scores.get(document_index, score)
            )
        return sigmoid_normalize(
            [document_scores.get(index, 0.0) for index in range(len(texts))]
        )

    def rerank(
        self, query: str, results: List[SearchResult], top_k: int
    ) -> List[SearchResult]:
        ranked = [result.model_copy(deep=True) for result in results]
        for result, score in zip(
            ranked, self.score(query, [result.text_for_rerank() for result in ranked])
        ):
            result.rerank_score = score
        return sorted(
            ranked, key=lambda result: result.rerank_score or 0.0, reverse=True
        )[:top_k]
