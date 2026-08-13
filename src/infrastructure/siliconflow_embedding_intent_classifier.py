"""Prototype-based Qwen3 embedding classifier for multi-label search routing."""
from __future__ import annotations

import math
from threading import Lock
from typing import Any, Iterable, Sequence

import requests

from src.application.ports.cache import CacheBackend
from src.domain.errors import ExternalServiceError
from src.domain.intent import IntentDecision, IntentSourceType
from src.infrastructure.http_errors import external_http_error
from src.infrastructure.http_timeout import bounded_http_timeout


_SOURCE_ORDER: tuple[IntentSourceType, ...] = ("academic", "patent", "legal")
_PROTOTYPES: dict[str, tuple[str, ...]] = {
    "academic": (
        "查找关于固态电池的学术论文和文献综述",
        "检索 DOI、期刊文章、会议论文或预印本",
        "有哪些同行评审研究支持这个结论",
        "搜索 arXiv 上的机器学习论文",
        "Find peer-reviewed papers and a literature review about this topic",
        "Search journal articles, DOI, citations and academic publications",
    ),
    "patent": (
        "查找固态电池相关专利、申请号和权利要求",
        "检索某技术的发明专利和专利族",
        "查询 IPC CPC 分类与专利申请人",
        "搜索 WIPO USPTO EPO 的专利文献",
        "Find patents, claims and publication numbers for this technology",
        "Search patent applications, inventors, assignees and prior art",
    ),
    "legal": (
        "查询中国法律法规、法条和司法解释",
        "民法典第五百零九条如何规定",
        "某项法规现行是否有效或已经废止",
        "查找最高人民法院司法解释和法律依据",
        "Find the applicable Chinese statute, regulation or judicial interpretation",
        "What does this law require and which legal article applies",
    ),
    # Hard negatives are deliberately close to rule keywords. They let the
    # relative-margin gate distinguish "legal name" and "invention history"
    # from actual legal and patent retrieval intents.
    "general": (
        "搜索今天的人工智能新闻",
        "介绍一家公司的产品和发展历史",
        "查询天气、人物、地点或普通事实",
        "某个词语是什么意思",
        "公司发表声明回应市场传闻",
        "电话是谁发明的以及发明历史",
        "What is the legal name of this company",
        "Read the legal notice and privacy page of this website",
        "History of the invention of the telephone",
        "Find recent news and general web information about this topic",
    ),
}


class SiliconFlowEmbeddingIntentClassifier:
    """Classify each vertical independently from Qwen3 embedding similarity.

    The first uncached request embeds the query and the static bilingual
    prototypes together. Later requests embed only the query. A vertical is a
    candidate when it clears both its absolute cosine threshold and a relative
    margin against the general-search centroid and its mapped score clears the
    configured source-confidence threshold. The application repeats that
    threshold as the final whole-decision abstention gate.
    """

    authoritative_routes = True

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "Qwen/Qwen3-Embedding-0.6B",
        *,
        cache: CacheBackend,
        http_session: Any = None,
        cache_ttl: int = 3600,
        timeout: int = 8,
        academic_threshold: float = 0.60,
        patent_threshold: float = 0.53,
        legal_threshold: float = 0.61,
        general_margin: float = 0.03,
        confidence_scale: float = 0.02,
        source_min_confidence: float = 0.70,
    ) -> None:
        thresholds = {
            "academic": float(academic_threshold),
            "patent": float(patent_threshold),
            "legal": float(legal_threshold),
        }
        if any(not 0.0 <= value <= 1.0 for value in thresholds.values()):
            raise ValueError("embedding intent thresholds must be between 0 and 1")
        if not 0.0 <= general_margin <= 1.0:
            raise ValueError("embedding intent general margin must be between 0 and 1")
        if confidence_scale <= 0:
            raise ValueError("embedding intent confidence scale must be positive")
        if not 0.0 <= source_min_confidence <= 1.0:
            raise ValueError(
                "embedding intent source confidence must be between 0 and 1"
            )
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._model = model
        self._cache = cache
        self._http = http_session or requests
        self._cache_ttl = cache_ttl
        self._timeout = timeout
        self._thresholds = thresholds
        self._general_margin = float(general_margin)
        self._confidence_scale = float(confidence_scale)
        self._source_min_confidence = float(source_min_confidence)
        self._centroids: dict[str, tuple[float, ...]] | None = None
        self._prototype_lock = Lock()

    def classify(self, query: str) -> IntentDecision:
        return self.classify_with_timeout(query)

    def classify_with_timeout(
        self,
        query: str,
        *,
        timeout_seconds: float | None = None,
    ) -> IntentDecision:
        key = "|".join((
            "intent-embedding:v1",
            self._model,
            *(f"{source}={self._thresholds[source]:.6f}" for source in _SOURCE_ORDER),
            f"margin={self._general_margin:.6f}",
            f"scale={self._confidence_scale:.6f}",
            f"source-confidence={self._source_min_confidence:.6f}",
            query,
        ))
        cached = self._cache.get(key)
        if cached is not None:
            if not isinstance(cached, IntentDecision):
                raise TypeError("intent cache value must be IntentDecision")
            return cached

        query_vector = self._query_vector(query, timeout_seconds=timeout_seconds)
        centroids = self._centroids
        if centroids is None:  # pragma: no cover - guarded by _query_vector
            raise RuntimeError("embedding intent prototypes were not initialized")
        similarities = {
            name: self._dot(query_vector, centroid)
            for name, centroid in centroids.items()
        }
        general_score = similarities["general"]
        probabilities: dict[IntentSourceType, float] = {}
        sources: list[IntentSourceType] = []
        for source in _SOURCE_ORDER:
            boundary_margin = min(
                similarities[source] - self._thresholds[source],
                similarities[source] - general_score + self._general_margin,
            )
            probability = self._sigmoid(boundary_margin / self._confidence_scale)
            probabilities[source] = probability
            if probability >= self._source_min_confidence:
                sources.append(source)

        if not sources:
            intent = "general_search"
            confidence = 1.0 - max(probabilities.values())
        elif len(sources) > 1:
            intent = "mixed_research"
            confidence = min(probabilities[source] for source in sources)
        else:
            only = sources[0]
            intent = {
                "academic": "academic_literature",
                "patent": "patent",
                "legal": "legal",
            }[only]
            confidence = probabilities[only]

        decision = IntentDecision(
            intent=intent,  # type: ignore[arg-type]
            source_types=tuple(sources),
            confidence=confidence,
            source_scores=tuple(
                (source, probabilities[source]) for source in _SOURCE_ORDER
            ),
        )
        self._cache.set(key, decision, self._cache_ttl)
        return decision

    def _query_vector(
        self,
        query: str,
        *,
        timeout_seconds: float | None,
    ) -> tuple[float, ...]:
        if self._centroids is not None:
            return self._embed((query,), timeout_seconds=timeout_seconds)[0]
        with self._prototype_lock:
            if self._centroids is not None:
                return self._embed((query,), timeout_seconds=timeout_seconds)[0]
            prototype_texts = tuple(
                text
                for name in (*_SOURCE_ORDER, "general")
                for text in _PROTOTYPES[name]
            )
            vectors = self._embed(
                (query, *prototype_texts),
                timeout_seconds=timeout_seconds,
            )
            query_vector, prototype_vectors = vectors[0], vectors[1:]
            centroids: dict[str, tuple[float, ...]] = {}
            offset = 0
            for name in (*_SOURCE_ORDER, "general"):
                size = len(_PROTOTYPES[name])
                centroids[name] = self._centroid(
                    prototype_vectors[offset:offset + size]
                )
                offset += size
            self._centroids = centroids
            return query_vector

    def _embed(
        self,
        inputs: Sequence[str],
        *,
        timeout_seconds: float | None,
    ) -> tuple[tuple[float, ...], ...]:
        try:
            response = self._http.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "input": list(inputs),
                    "encoding_format": "float",
                },
                timeout=bounded_http_timeout(self._timeout, timeout_seconds),
            )
            response.raise_for_status()
            return self._parse_embeddings(response.json(), expected=len(inputs))
        except ExternalServiceError:
            raise
        except Exception as exc:
            raise external_http_error(
                "siliconflow", "intent_embedding", exc
            ) from exc

    @classmethod
    def _parse_embeddings(
        cls,
        payload: object,
        *,
        expected: int,
    ) -> tuple[tuple[float, ...], ...]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("embedding response must contain a data array")
        indexed: dict[int, tuple[float, ...]] = {}
        dimension: int | None = None
        for item in payload["data"]:
            if not isinstance(item, dict):
                raise ValueError("embedding data item must be an object")
            index = item.get("index")
            embedding = item.get("embedding")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < expected
                or index in indexed
            ):
                raise ValueError("embedding response contains an invalid index")
            if not isinstance(embedding, list) or not embedding:
                raise ValueError("embedding vector must be a non-empty array")
            vector = tuple(
                float(value)
                for value in embedding
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            )
            if len(vector) != len(embedding) or any(
                not math.isfinite(value) for value in vector
            ):
                raise ValueError("embedding vector must contain finite numbers")
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise ValueError("embedding vectors must have one dimension")
            indexed[index] = cls._normalize(vector)
        if len(indexed) != expected:
            raise ValueError("embedding response count does not match input count")
        return tuple(indexed[index] for index in range(expected))

    @staticmethod
    def _normalize(vector: Iterable[float]) -> tuple[float, ...]:
        values = tuple(float(value) for value in vector)
        magnitude = math.sqrt(sum(value * value for value in values))
        if magnitude <= 0.0:
            raise ValueError("embedding vector magnitude must be positive")
        return tuple(value / magnitude for value in values)

    @classmethod
    def _centroid(
        cls,
        vectors: Sequence[Sequence[float]],
    ) -> tuple[float, ...]:
        if not vectors:
            raise ValueError("embedding prototype group must not be empty")
        return cls._normalize(
            sum(values) / len(vectors) for values in zip(*vectors)
        )

    @staticmethod
    def _dot(left: Sequence[float], right: Sequence[float]) -> float:
        return sum(a * b for a, b in zip(left, right))

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            factor = math.exp(-value)
            return 1.0 / (1.0 + factor)
        factor = math.exp(value)
        return factor / (1.0 + factor)
