"""BGE cross-encoder text-scoring adapter."""
from __future__ import annotations

import threading
from typing import List, Optional, Sequence

from src.models import SearchResult
from src.pipeline.chunk import chunk_text
from src.ranking.ports import Reranker, sigmoid_normalize


class BGEReranker(Reranker):
    name = "bge"

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        max_length: int = 512,
        device: Optional[str] = None,
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ) -> None:
        from sentence_transformers import CrossEncoder

        kwargs = {"max_length": max_length}
        if device:
            kwargs["device"] = device
        self._model = CrossEncoder(model_name, **kwargs)
        self._lock = threading.Lock()
        self.name = f"bge:{model_name.split('/')[-1]}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
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
        with self._lock:
            scores = self._model.predict([(query, text) for _, text in pairs])
        document_scores: dict[int, float] = {}
        for (document_index, _), raw_score in zip(pairs, scores):
            score = float(raw_score)
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
