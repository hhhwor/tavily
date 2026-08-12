"""Qwen3 intent classifier adapter and planning integration tests."""
from __future__ import annotations

import pytest

from src.application.commands import SearchCommand
from src.application.query_planner import QueryPlanner
from src.config import Settings
from src.domain.errors import ExternalServiceError
from src.domain.intent import IntentDecision
from src.infrastructure.cache import InMemoryCache
from src.infrastructure.siliconflow_intent_classifier import (
    SiliconFlowIntentClassifier,
)


def _settings(**overrides) -> Settings:
    values = {
        "openalex_enabled": False,
        "patent_es_enabled": False,
        "ranking_profile": "fast",
        "rerank_threshold_mode": "off",
        "mcp_mode": "false",
        "siliconflow_api_key": "test-key",
        "intent_classifier_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_classifier_requests_qwen3_json_mode_without_thinking_and_caches():
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '''{
                "intent": "mixed_research",
                "source_types": ["patent", "academic"],
                "confidence": 0.92,
                "legal_mode": null
            }'''}}]}

    class Session:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Response()

    classifier = SiliconFlowIntentClassifier(
        "secret",
        "https://example.test/v1",
        "Qwen/Qwen3-8B",
        cache=InMemoryCache(),
        http_session=Session(),
    )

    first = classifier.classify("查固态电池论文和专利")
    second = classifier.classify("查固态电池论文和专利")

    assert first == IntentDecision(
        intent="mixed_research",
        source_types=("academic", "patent"),
        confidence=0.92,
    )
    assert second == first
    assert len(calls) == 1
    payload = calls[0][1]["json"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["enable_thinking"] is False
    assert payload["temperature"] == 0.0


@pytest.mark.parametrize(
    "content",
    [
        '{"intent":"patENT","source_types":["patent"],"confidence":0.9,"legal_mode":null}',
        '{"intent":"legal","source_types":["legal"],"confidence":"high","legal_mode":null}',
        '{"intent":"mixed_research","source_types":["academic"],"confidence":0.9,"legal_mode":null}',
    ],
)
def test_classifier_rejects_invalid_model_enums_or_shapes(content):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    classifier = SiliconFlowIntentClassifier(
        "secret",
        "https://example.test/v1",
        "Qwen/Qwen3-8B",
        cache=InMemoryCache(),
        http_session=Session(),
    )

    with pytest.raises(ExternalServiceError) as error:
        classifier.classify("test")

    assert error.value.code == "INTENT_CLASSIFICATION_INVALID_RESPONSE"


def test_planner_uses_high_confidence_model_sources_and_keeps_rule_routes():
    class Classifier:
        def __init__(self):
            self.calls = []

        def classify(self, query):
            self.calls.append(query)
            return IntentDecision(
                intent="mixed_research",
                source_types=("academic", "patent"),
                confidence=0.91,
            )

    classifier = Classifier()
    planner = QueryPlanner(_settings(), intent_classifier=classifier)

    planned = planner.plan(
        SearchCommand("比较固态电池的发展"),
        ("web",),
        academic_available=True,
        patent_available=True,
        legal_available=True,
    )

    assert classifier.calls == ["比较固态电池的发展"]
    assert planned.do_academic is True
    assert planned.do_patent is True
    assert planned.do_legal is False
    assert planned.plan.intent == "mixed_research"
    assert planned.plan.intent_confidence == 0.91


def test_planner_skips_model_for_explicit_source_types_and_low_confidence():
    class Classifier:
        def __init__(self):
            self.calls = []

        def classify(self, query):
            self.calls.append(query)
            return IntentDecision(
                intent="legal",
                source_types=("legal",),
                confidence=0.69,
                legal_mode="general",
            )

    classifier = Classifier()
    planner = QueryPlanner(_settings(), intent_classifier=classifier)

    explicit = planner.plan(
        SearchCommand("请找论文", source_types=("academic",)),
        (),
        academic_available=True,
        patent_available=True,
        legal_available=True,
    )
    low_confidence = planner.plan(
        SearchCommand("比较固态电池的发展"),
        (),
        academic_available=True,
        patent_available=True,
        legal_available=True,
    )

    assert explicit.do_academic is True
    assert classifier.calls == ["比较固态电池的发展"]
    assert low_confidence.do_legal is False
    assert low_confidence.plan.intent is None


def test_planner_falls_back_to_rules_when_classifier_fails():
    class FailingClassifier:
        def classify(self, query):
            raise RuntimeError("upstream unavailable")

    planned = QueryPlanner(
        _settings(), intent_classifier=FailingClassifier()
    ).plan(
        SearchCommand("民法典第五百零九条"),
        (),
        academic_available=False,
        patent_available=False,
        legal_available=True,
    )

    assert planned.do_legal is True
    assert [(failure.stage, failure.code) for failure in planned.failures] == [
        ("intent_classification", "INTENT_CLASSIFICATION_FAILED"),
    ]
