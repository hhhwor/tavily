"""Qwen3 embedding intent classifier adapter tests."""
from __future__ import annotations

import math

import pytest

from src.domain.errors import ExternalServiceError
from src.domain.intent import IntentDecision
from src.infrastructure.cache import InMemoryCache
from src.infrastructure.siliconflow_embedding_intent_classifier import (
    SiliconFlowEmbeddingIntentClassifier,
)


_CALIBRATION = {
    "artifact_version": 1,
    "classifier": "independent_logistic_heads",
    "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
    "embedding_dimension": 4,
    "query_prefix": "route: ",
    "source_order": ["academic", "patent", "legal"],
    "thresholds": {"academic": 0.5, "patent": 0.5, "legal": 0.5},
    "weights": {
        "academic": [12.0, 0.0, 0.0, 0.0],
        "patent": [0.0, 12.0, 0.0, 0.0],
        "legal": [0.0, 0.0, 12.0, 0.0],
    },
    "bias": {"academic": -6.0, "patent": -6.0, "legal": -6.0},
    "training": {"corpus_sha256": "test-artifact"},
}


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_classifier_runs_linear_heads_classifies_multilabel_and_caches():
    calls = []

    class Session:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            inputs = kwargs["json"]["input"]
            vectors = []
            for text in inputs:
                if text == "route: 查找论文和专利":
                    vector = [1.0, 1.0, 0.0, 0.0]
                elif text == "route: 今天的普通新闻":
                    vector = [0.0, 0.0, 0.0, 1.0]
                else:  # pragma: no cover - protects test fixture evolution
                    raise AssertionError(f"unexpected embedding input: {text}")
                vectors.append(vector)
            return _Response({
                "data": [
                    {"index": index, "embedding": vector}
                    for index, vector in reversed(list(enumerate(vectors)))
                ]
            })

    classifier = SiliconFlowEmbeddingIntentClassifier(
        "secret",
        "https://example.test/v1",
        cache=InMemoryCache(),
        http_session=Session(),
        calibration=_CALIBRATION,
    )

    mixed = classifier.classify("查找论文和专利")
    cached = classifier.classify("查找论文和专利")
    general = classifier.classify("今天的普通新闻")

    assert mixed.intent == "mixed_research"
    assert mixed.source_types == ("academic", "patent")
    assert mixed.confidence > 0.90
    assert dict(mixed.source_scores)["legal"] < 0.01
    assert cached == mixed
    assert general.intent == "general_search"
    assert general.source_types == ()
    assert general.confidence > 0.99
    assert len(calls) == 2
    first_payload = calls[0][1]["json"]
    second_payload = calls[1][1]["json"]
    assert first_payload["model"] == "Qwen/Qwen3-Embedding-0.6B"
    assert first_payload["encoding_format"] == "float"
    assert first_payload["input"] == ["route: 查找论文和专利"]
    assert second_payload["input"] == ["route: 今天的普通新闻"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": []},
        {"data": [{"index": 0, "embedding": []}]},
        {"data": [{"index": 0, "embedding": [math.nan]}]},
    ],
)
def test_classifier_rejects_malformed_embedding_responses(payload):
    class Session:
        def post(self, *args, **kwargs):
            return _Response(payload)

    classifier = SiliconFlowEmbeddingIntentClassifier(
        "secret",
        "https://example.test/v1",
        cache=InMemoryCache(),
        http_session=Session(),
        calibration=_CALIBRATION,
    )

    with pytest.raises(ExternalServiceError) as error:
        classifier.classify("query")

    assert error.value.code == "INTENT_EMBEDDING_INVALID_RESPONSE"


def test_intent_decision_validates_and_orders_embedding_source_scores():
    decision = IntentDecision(
        intent="mixed_research",
        source_types=("legal", "academic"),
        confidence=0.9,
        source_scores=(("legal", 0.8), ("academic", 0.9), ("patent", 0.1)),
    )

    assert decision.source_types == ("academic", "legal")
    assert decision.source_scores == (
        ("academic", 0.9),
        ("patent", 0.1),
        ("legal", 0.8),
    )
    with pytest.raises(ValueError, match="source_scores"):
        IntentDecision(
            intent="academic_literature",
            source_types=("academic",),
            confidence=0.9,
            source_scores=(("academic", 1.1),),
        )
