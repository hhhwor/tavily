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


def test_planner_derives_effective_intent_after_merging_rule_and_model_routes():
    class Classifier:
        def classify(self, query):
            return IntentDecision(
                intent="academic_literature",
                source_types=("academic",),
                confidence=0.91,
                source_scores=(("academic", 0.91), ("legal", 0.2)),
            )

    planned = QueryPlanner(
        _settings(), intent_classifier=Classifier()
    ).plan(
        SearchCommand("民法典相关学术观点"),
        ("web",),
        academic_available=True,
        patent_available=True,
        legal_available=True,
    )

    assert planned.do_academic is True
    assert planned.do_legal is True
    assert planned.plan.intent == "mixed_research"
    assert planned.plan.intent_confidence is None
    assert planned.plan.intent_source_scores == (
        ("academic", 0.91),
        ("legal", 0.2),
    )


def test_planner_does_not_expand_two_rule_routes_with_a_third_model_source():
    class Classifier:
        def classify(self, query):
            return IntentDecision(
                intent="mixed_research",
                source_types=("academic", "patent", "legal"),
                confidence=0.9,
            )

    planned = QueryPlanner(
        _settings(), intent_classifier=Classifier()
    ).plan(
        SearchCommand("查找算法治理相关论文和中国法规"),
        ("web",),
        academic_available=True,
        patent_available=True,
        legal_available=True,
    )

    assert planned.do_academic is True
    assert planned.do_legal is True
    assert planned.do_patent is False
    assert planned.plan.intent == "mixed_research"


def test_planner_calibrated_embedding_can_add_third_route_to_two_rules():
    class Classifier:
        authoritative_routes = True
        owns_thresholds = True

        def classify(self, query):
            return IntentDecision(
                intent="mixed_research",
                source_types=("academic", "patent", "legal"),
                confidence=0.59,
            )

    planned = QueryPlanner(
        _settings(), intent_classifier=Classifier()
    ).plan(
        SearchCommand("查找算法治理相关论文和中国法规"),
        ("web",),
        academic_available=True,
        patent_available=True,
        legal_available=True,
    )

    assert planned.do_academic is True
    assert planned.do_legal is True
    assert planned.do_patent is True
    assert planned.plan.intent == "mixed_research"


def test_planner_accepts_authoritative_embedding_general_over_one_noisy_rule():
    class EmbeddingClassifier:
        authoritative_routes = True

        def classify(self, query):
            return IntentDecision(
                intent="general_search",
                source_types=(),
                confidence=0.99,
            )

    planner = QueryPlanner(
        _settings(), intent_classifier=EmbeddingClassifier()
    )
    cases = (
        ("公司发表声明", "academic"),
        ("history of the invention of the telephone", "patent"),
        ("What is the legal name of OpenAI?", "legal"),
    )

    for query, noisy_source in cases:
        planned = planner.plan(
            SearchCommand(query),
            ("web",),
            academic_available=True,
            patent_available=True,
            legal_available=True,
        )

        assert planned.plan.intent == "general_search"
        assert planned.active_provider_names == ("web",)
        assert getattr(planned, f"do_{noisy_source}") is False


def test_planner_uses_embedding_owned_threshold_below_chat_confidence_gate():
    class EmbeddingClassifier:
        authoritative_routes = True
        owns_thresholds = True

        def classify(self, query):
            return IntentDecision(
                intent="legal",
                source_types=("legal",),
                confidence=0.59,
                source_scores=(("legal", 0.59),),
            )

    planned = QueryPlanner(
        _settings(), intent_classifier=EmbeddingClassifier()
    ).plan(
        SearchCommand("比较平台治理方案"),
        ("web",),
        academic_available=True,
        patent_available=True,
        legal_available=True,
    )

    assert planned.do_legal is True
    assert planned.plan.intent == "legal"
    assert planned.plan.intent_confidence == 0.59


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
