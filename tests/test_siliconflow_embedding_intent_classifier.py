"""Qwen3 embedding intent classifier adapter tests."""
from __future__ import annotations

import math

import pytest

from src.domain.errors import ExternalServiceError
from src.domain.intent import IntentDecision
from src.infrastructure.cache import InMemoryCache
from src.infrastructure.siliconflow_embedding_intent_classifier import (
    _PROTOTYPES,
    SiliconFlowEmbeddingIntentClassifier,
)


_CLASS_VECTORS = {
    "academic": [1.0, 0.0, 0.0, 0.0],
    "patent": [0.0, 1.0, 0.0, 0.0],
    "legal": [0.0, 0.0, 1.0, 0.0],
    "general": [0.0, 0.0, 0.0, 1.0],
}


def _prototype_class(text: str) -> str | None:
    return next(
        (name for name, examples in _PROTOTYPES.items() if text in examples),
        None,
    )


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_classifier_builds_prototypes_once_classifies_multilabel_and_caches():
    calls = []

    class Session:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            inputs = kwargs["json"]["input"]
            vectors = []
            for text in inputs:
                prototype_class = _prototype_class(text)
                if prototype_class is not None:
                    vector = _CLASS_VECTORS[prototype_class]
                elif text == "查找论文和专利":
                    vector = [1.0, 1.0, 0.0, 0.0]
                elif text == "今天的普通新闻":
                    vector = _CLASS_VECTORS["general"]
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
    )

    mixed = classifier.classify("查找论文和专利")
    cached = classifier.classify("查找论文和专利")
    general = classifier.classify("今天的普通新闻")

    assert mixed.intent == "mixed_research"
    assert mixed.source_types == ("academic", "patent")
    assert mixed.confidence > 0.99
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
    assert len(first_payload["input"]) == 1 + sum(map(len, _PROTOTYPES.values()))
    assert second_payload["input"] == ["今天的普通新闻"]


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
