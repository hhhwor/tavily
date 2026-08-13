"""Calibrated Qwen3 embedding classifier for multi-label search routing."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import requests

from src.application.ports.cache import CacheBackend
from src.domain.errors import ExternalServiceError
from src.domain.intent import IntentDecision, IntentSourceType
from src.infrastructure.http_errors import external_http_error
from src.infrastructure.http_timeout import bounded_http_timeout


_SOURCE_ORDER: tuple[IntentSourceType, ...] = ("academic", "patent", "legal")
_DEFAULT_CALIBRATION_PATH = (
    Path(__file__).with_name("data") / "qwen3_embedding_intent_linear_v1.json"
)


class SiliconFlowEmbeddingIntentClassifier:
    """Run three calibrated logistic heads over a frozen Qwen3 embedding.

    Training and threshold selection are offline; production loads a versioned
    artifact containing one independent linear head and threshold per source.
    Each uncached classification therefore makes one embedding request for one
    instruction-prefixed query.  ``owns_thresholds`` tells the application that
    these validation-calibrated thresholds replace its legacy whole-decision
    confidence gate.
    """

    authoritative_routes = True
    owns_thresholds = True

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
        academic_threshold: float | None = None,
        patent_threshold: float | None = None,
        legal_threshold: float | None = None,
        calibration_path: str | Path | None = None,
        calibration: Mapping[str, object] | None = None,
    ) -> None:
        artifact = dict(calibration) if calibration is not None else self._load_artifact(
            Path(calibration_path) if calibration_path else _DEFAULT_CALIBRATION_PATH
        )
        parsed = self._parse_artifact(artifact, model=model)
        artifact_thresholds, weights, bias, dimension, prefix, artifact_id = parsed
        overrides = {
            "academic": academic_threshold,
            "patent": patent_threshold,
            "legal": legal_threshold,
        }
        thresholds = {
            source: (
                artifact_thresholds[source]
                if overrides[source] is None
                else float(overrides[source])
            )
            for source in _SOURCE_ORDER
        }
        if any(not 0.0 <= value <= 1.0 for value in thresholds.values()):
            raise ValueError("embedding intent thresholds must be between 0 and 1")

        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._model = model
        self._cache = cache
        self._http = http_session or requests
        self._cache_ttl = cache_ttl
        self._timeout = timeout
        self._thresholds = thresholds
        self._weights = weights
        self._bias = bias
        self._dimension = dimension
        self._query_prefix = prefix
        self._artifact_id = artifact_id

    def classify(self, query: str) -> IntentDecision:
        return self.classify_with_timeout(query)

    def classify_with_timeout(
        self,
        query: str,
        *,
        timeout_seconds: float | None = None,
    ) -> IntentDecision:
        key = "|".join((
            "intent-embedding:linear:v1",
            self._model,
            self._artifact_id,
            *(f"{source}={self._thresholds[source]:.6f}" for source in _SOURCE_ORDER),
            query,
        ))
        cached = self._cache.get(key)
        if cached is not None:
            if not isinstance(cached, IntentDecision):
                raise TypeError("intent cache value must be IntentDecision")
            return cached

        query_vector = self._embed(
            (self._query_prefix + query,), timeout_seconds=timeout_seconds
        )[0]
        probabilities: dict[IntentSourceType, float] = {
            source: self._sigmoid(
                self._dot(query_vector, self._weights[source])
                + self._bias[source]
            )
            for source in _SOURCE_ORDER
        }
        sources = tuple(
            source for source in _SOURCE_ORDER
            if probabilities[source] >= self._thresholds[source]
        )

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
            source_types=sources,
            confidence=confidence,
            source_scores=tuple(
                (source, probabilities[source]) for source in _SOURCE_ORDER
            ),
        )
        self._cache.set(key, decision, self._cache_ttl)
        return decision

    @staticmethod
    def _load_artifact(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(
                f"failed to load embedding intent calibration: {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("embedding intent calibration must be a JSON object")
        return payload

    @classmethod
    def _parse_artifact(
        cls,
        artifact: Mapping[str, object],
        *,
        model: str,
    ) -> tuple[
        dict[IntentSourceType, float],
        dict[IntentSourceType, tuple[float, ...]],
        dict[IntentSourceType, float],
        int,
        str,
        str,
    ]:
        if artifact.get("artifact_version") != 1:
            raise ValueError("unsupported embedding intent artifact version")
        if artifact.get("classifier") != "independent_logistic_heads":
            raise ValueError("unsupported embedding intent classifier")
        if artifact.get("embedding_model") != model:
            raise ValueError(
                "embedding intent artifact model does not match configured model"
            )
        if artifact.get("source_order") != list(_SOURCE_ORDER):
            raise ValueError("embedding intent artifact source order is invalid")
        dimension = artifact.get("embedding_dimension")
        prefix = artifact.get("query_prefix")
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or dimension <= 0
            or not isinstance(prefix, str)
        ):
            raise ValueError("embedding intent artifact metadata is invalid")
        raw_thresholds = artifact.get("thresholds")
        raw_weights = artifact.get("weights")
        raw_bias = artifact.get("bias")
        if not all(isinstance(value, Mapping) for value in (
            raw_thresholds, raw_weights, raw_bias
        )):
            raise ValueError("embedding intent artifact heads are invalid")

        thresholds: dict[IntentSourceType, float] = {}
        weights: dict[IntentSourceType, tuple[float, ...]] = {}
        bias: dict[IntentSourceType, float] = {}
        for source in _SOURCE_ORDER:
            threshold = cls._finite_float(raw_thresholds.get(source))
            vector = raw_weights.get(source)
            source_bias = cls._finite_float(raw_bias.get(source))
            if not 0.0 <= threshold <= 1.0:
                raise ValueError("embedding intent artifact threshold is invalid")
            if not isinstance(vector, list) or len(vector) != dimension:
                raise ValueError("embedding intent artifact weight dimension is invalid")
            parsed_vector = tuple(cls._finite_float(value) for value in vector)
            thresholds[source] = threshold
            weights[source] = parsed_vector
            bias[source] = source_bias

        training = artifact.get("training")
        artifact_id = "unknown"
        if isinstance(training, Mapping):
            corpus_hash = training.get("corpus_sha256")
            if isinstance(corpus_hash, str) and corpus_hash:
                artifact_id = corpus_hash[:16]
        return thresholds, weights, bias, dimension, prefix, artifact_id

    @staticmethod
    def _finite_float(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("embedding intent artifact value must be numeric")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("embedding intent artifact value must be finite")
        return parsed

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
            vectors = self._parse_embeddings(response.json(), expected=len(inputs))
            if any(len(vector) != self._dimension for vector in vectors):
                raise ValueError(
                    "embedding response dimension does not match classifier artifact"
                )
            return vectors
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
