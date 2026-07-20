"""F03 QueryPlanner 与 application 阶段契约测试。"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from dataclasses import fields
from inspect import signature

import pytest

from src.application.commands import SearchCommand
from src.application.outcomes import PdfEnrichmentOutcome, RecallOutcome
from src.application.query_planner import QueryPlanner
from src.config import Settings
from src.domain.documents import EnrichedDocument, RankedDocument, RetrievedDocument
from src.engine import SearchEngine
from src.models import AcademicResult, SearchFailure, SearchPlan


def _settings(**overrides) -> Settings:
    values = {
        "default_top_k": 7,
        "openalex_enabled": False,
        "patent_es_enabled": False,
        "ranking_profile": "fast",
        "rerank_threshold_mode": "off",
        "mcp_mode": "false",
    }
    values.update(overrides)
    return Settings(**values)


def test_search_command_is_frozen_and_has_lightweight_defaults():
    command = SearchCommand("query")

    assert command.limit == 10
    assert command.source_types is None
    assert command.filters.languages == ()
    with pytest.raises(FrozenInstanceError):
        command.limit = 3  # type: ignore[misc]


def test_search_command_matches_current_search_engine_parameters():
    command_fields = {field.name for field in fields(SearchCommand)}
    engine_parameters = set(signature(SearchEngine.search).parameters) - {"self"}

    assert command_fields == engine_parameters


def test_planner_preserves_l0_normalization_detection_and_default_top_k():
    planner = QueryPlanner(_settings())
    planned = planner.plan(
        SearchCommand("  最新  AI 论文和专利？"),
        ("tencent", "baidu"),
        academic_available=True,
        patent_available=True,
    )

    assert planned.plan.normalized_query == "最新 AI 论文和专利"
    assert planned.plan.recency == "month"
    assert planned.plan.time_sensitive is True
    assert planned.plan.top_k == 10
    assert planned.active_provider_names == ("tencent", "baidu")
    assert planned.do_academic is True
    assert planned.do_patent is True
    assert planned.search_query == "最新 AI 论文和专利"
    assert planned.academic_query == planned.search_query
    assert planned.failures == ()


def test_planner_forwards_request_overrides_to_l0():
    calls = []

    def fake_plan(query, providers, top_k, **kwargs):
        calls.append((query, providers, top_k, kwargs))
        return SearchPlan(
            raw_query=query,
            normalized_query="normalized",
            providers=["baidu"],
            top_k=top_k,
            academic=False,
            patent=False,
        )

    planner = QueryPlanner(
        _settings(rewrite_enabled=True),
        plan_query_fn=fake_plan,
    )
    planned = planner.plan(
        SearchCommand("raw", limit=4, source_types=("web", "patent")),
        ("tencent", "baidu"),
        academic_available=True,
        patent_available=True,
    )

    query, providers, top_k, kwargs = calls[0]
    assert (query, providers, top_k) == ("raw", ["tencent", "baidu"], 4)
    assert kwargs["rewrite"] is False
    assert kwargs["force_academic"] is False
    assert kwargs["force_patent"] is True
    assert "http_session" not in kwargs
    assert planned.active_provider_names == ("baidu",)


def test_planner_reports_unavailable_verticals_without_rewriting_academic():
    academic_rewrite_calls = []

    class Rewriter:
        def rewrite(self, query, *, academic=False):
            academic_rewrite_calls.append((query, academic))
            return "unexpected"

    planner = QueryPlanner(
        _settings(
            siliconflow_api_key="configured",
            openalex_query_rewrite=True,
        ),
        Rewriter(),
    )
    planned = planner.plan(
        SearchCommand("论文和专利", source_types=("academic", "patent")),
        (),
        academic_available=False,
        patent_available=False,
    )

    assert planned.do_academic is False
    assert planned.do_patent is False
    assert planned.academic_query == planned.search_query
    assert academic_rewrite_calls == []
    assert [(failure.stage, failure.source, failure.type, failure.code) for failure in planned.failures] == [
        ("routing", "openalex_local", "academic", "PROVIDER_UNAVAILABLE"),
        ("routing", "patent_es", "patent", "PROVIDER_UNAVAILABLE"),
    ]


def test_planner_combines_plan_and_academic_rewrite_failures():
    initial_failure = SearchFailure(
        stage="query_rewrite",
        source="siliconflow",
        code="QUERY_REWRITE_FAILED",
        message="general rewrite failed",
    )

    def fake_plan(query, providers, top_k, **kwargs):
        return SearchPlan(
            raw_query=query,
            normalized_query="normalized query",
            academic=True,
            providers=list(providers),
            top_k=top_k,
            failures=[initial_failure],
        )

    class FailingAcademicRewriter:
        def rewrite(self, query, *, academic=False):
            assert query == "normalized query"
            assert academic is True
            raise RuntimeError("academic rewrite failed")

    planner = QueryPlanner(
        _settings(
            siliconflow_api_key="configured",
            openalex_query_rewrite=True,
        ),
        FailingAcademicRewriter(),
        plan_query_fn=fake_plan,
    )
    planned = planner.plan(
        SearchCommand("raw"),
        ("baidu",),
        academic_available=True,
        patent_available=False,
    )

    assert [failure.code for failure in planned.failures] == [
        "QUERY_REWRITE_FAILED",
        "ACADEMIC_QUERY_REWRITE_FAILED",
    ]
    # L0 计划自身保持原有失败；组合 Outcome 承载后续阶段新增失败。
    assert [failure.code for failure in planned.plan.failures] == [
        "QUERY_REWRITE_FAILED"
    ]


def test_outcomes_accept_sequences_but_freeze_them_as_tuples():
    paper = AcademicResult(url="https://example.test/paper", title="paper")
    retrieved = RetrievedDocument.from_result(paper, "academic")
    ranked = RankedDocument(retrieved, None, "fast")
    enriched = EnrichedDocument.from_result(ranked, paper)
    failure = SearchFailure(stage="pdf_enrichment", code="FAILED")

    pdf = PdfEnrichmentOutcome(academic=[enriched], failures=[failure])  # type: ignore[arg-type]
    recall = RecallOutcome(academic=[retrieved], providers_used=["openalex_local"])  # type: ignore[arg-type]

    assert pdf.academic == (enriched,)
    assert pdf.failures == (failure,)
    assert recall.academic == (retrieved,)
    assert recall.providers_used == ("openalex_local",)
    with pytest.raises(FrozenInstanceError):
        pdf.academic = ()  # type: ignore[misc]
