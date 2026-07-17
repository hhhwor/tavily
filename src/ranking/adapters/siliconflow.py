"""SiliconFlow rerank HTTP adapter."""
from __future__ import annotations

from typing import Any, List, Sequence

import requests

from src.infrastructure.http_errors import external_http_error
from src.models import SearchResult
from src.pipeline.chunk import chunk_text
from src.ranking.ports import Reranker, clamp01


class SiliconFlowReranker(Reranker):
    name = "siliconflow"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "BAAI/bge-reranker-v2-m3",
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
        http_session: Any = None,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/rerank"
        self._model = model
        self.name = f"siliconflow:{model.split('/')[-1]}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap
        self._http = http_session or requests

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
        try:
            response = self._http.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "query": query,
                    "documents": [text for _, text in pairs],
                    "top_n": len(pairs),
                    "return_documents": False,
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise external_http_error("siliconflow", "rerank", exc) from exc

        document_scores: dict[int, float] = {}
        for item in data.get("results", []):
            document_index = pairs[item["index"]][0]
            score = float(item["relevance_score"])
            document_scores[document_index] = max(
                score, document_scores.get(document_index, score)
            )
        return [
            clamp01(document_scores.get(index, 0.0))
            for index in range(len(texts))
        ]

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
