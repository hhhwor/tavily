"""F03 召回、排序、用例编排与门面边界测试。"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.application.commands import SearchCommand
from src.application.outcomes import (
    DiscoveryOutcome,
    PlannedQuery,
    RankingOutcome,
    RecallOutcome,
)
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.search_service import SearchService
from src.application.source_registry import SourceRegistry
from src.application.trust_annotator import TrustOutcome
from src.application.ports.retrieval import SourceDescriptor
from src.cache import InMemoryCache
from src.config import Settings
from src.models import Answerability, SearchFailure, SearchPlan, SearchResult
from src.pipeline.ranking_options import resolve_ranking_options
from src.pipeline.rerank import NoOpReranker
from src.providers.base import SearchProvider
from src.infrastructure.runtime import SystemClock


class _InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class _Provider(SearchProvider):
    def __init__(self, name="web", *, failure=None):
        self.name = name
        self.descriptor = SourceDescriptor(
            id=name,
            kind="web",
            count_empty_as_used=True,
        )
        self.failure = failure
        self.calls = []

    def search(self, query, top_k, recency):
        self.calls.append((query, top_k, recency))
        if self.failure:
            raise self.failure
        return [SearchResult(url="https://example.test", title="original", source=self.name)]


def _settings(**overrides):
    values = {
        "openalex_enabled": False,
        "patent_es_enabled": False,
        "ranking_profile": "fast",
        "rerank_threshold_mode": "off",
        "mcp_mode": "false",
    }
    values.update(overrides)
    return Settings(**values)


def _planned(*, time_sensitive=False):
    plan = SearchPlan(
        raw_query="query",
        normalized_query="query",
        providers=["web"],
        top_k=3,
        time_sensitive=time_sensitive,
    )
    return PlannedQuery(
        plan=plan,
        search_query="query",
        academic_query="query",
        active_provider_names=("web",),
    )


def test_recall_cache_isolated_from_pipeline_mutation_and_time_sensitive_bypass():
    provider = _Provider()
    coordinator = RecallCoordinator(
        _settings(),
        SourceRegistry([provider]),
        InMemoryCache(),
        _InlineExecutor(),
        clock=SystemClock().now,
    )

    first = coordinator.recall(_planned())
    with pytest.raises(FrozenInstanceError):
        first.web[0].title = "mutated"  # type: ignore[misc]
    second = coordinator.recall(_planned())

    assert len(provider.calls) == 1
    assert second.web[0].title == "original"
    assert second.web[0] is first.web[0]
    assert second.web[0].primary_provider_rank == 0
    assert second.providers_used == ("web",)

    coordinator.recall(_planned(time_sensitive=True))
    coordinator.recall(_planned(time_sensitive=True))
    assert len(provider.calls) == 3


def test_recall_converts_provider_exception_to_stage_failure():
    provider = _Provider(failure=RuntimeError("unavailable"))
    outcome = RecallCoordinator(
        _settings(cache_enabled=False),
        SourceRegistry([provider]),
        None,
        _InlineExecutor(),
        clock=SystemClock().now,
    ).recall(_planned())

    assert outcome.web == ()
    assert outcome.providers_used == ()
    assert outcome.failures[0].stage == "provider_search"
    assert outcome.failures[0].source == "web"
    assert outcome.failures[0].code == "PROVIDER_SEARCH_FAILED"


def test_ranking_noop_disables_threshold_without_partial_failure():
    service = RankingService(
        _settings(ranking_profile="quality", rerank_threshold_mode="strict"),
        NoOpReranker(),
        lambda *_: NoOpReranker(),
        _InlineExecutor(),
        clock=SystemClock(),
    )
    outcome = service.rank(
        SearchCommand("query"),
        _planned(),
        RecallOutcome(web=(SearchResult(url="u", title="doc"),)),
    )

    assert outcome.options.profile == "quality"
    assert outcome.options.threshold_mode == "off"
    assert outcome.options.warnings == ("THRESHOLD_SKIPPED_NO_SCORER",)
    assert outcome.failures == ()


def test_ranking_failure_falls_back_per_domain(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("ranker down")

    monkeypatch.setattr(
        "src.application.ranking_service.WebReranker.rerank_with_context",
        fail,
    )
    original = SearchResult(url="u", title="doc")
    outcome = RankingService(
        _settings(),
        NoOpReranker(),
        lambda *_: NoOpReranker(),
        _InlineExecutor(),
        clock=SystemClock(),
    ).rank(SearchCommand("query"), _planned(), RecallOutcome(web=(original,)))

    assert outcome.web[0].to_result() == original
    assert outcome.failures[0].stage == "rerank"
    assert outcome.failures[0].source == "web_reranker"


def test_search_service_owns_stage_order_and_failure_order():
    trace = []
    saved_snapshots = []
    failures = [
        SearchFailure(stage=stage, code=stage.upper())
        for stage in ("plan", "recall", "rank")
    ]
    planned = PlannedQuery(
        plan=SearchPlan(raw_query="q", normalized_query="q", top_k=2),
        search_query="q",
        academic_query="q",
        active_provider_names=("web",),
        do_academic=True,
        failures=(failures[0],),
    )
    recalled = RecallOutcome(failures=(failures[1],))
    options = resolve_ranking_options(
        default_profile="fast",
        default_threshold=0.3,
        default_threshold_mode="off",
    )
    ranked = RankingOutcome(options=options, reranker="none", failures=(failures[2],))

    class Discovery:
        def execute(self, command):
            trace.append("discovery")
            return DiscoveryOutcome(
                query_time=SystemClock().now(),
                planned=planned,
                recalled=recalled,
                ranked=ranked,
            )

    class Evidence:
        def assemble(self, *args):
            trace.append("evidence")
            return []

    class Trust:
        def annotate(self, **kwargs):
            trace.append("trust")
            return TrustOutcome((), None)

    class Policy:
        def evaluate(self, *args, **kwargs):
            trace.append("answer")
            return Answerability()

    class SeedStore:
        def save(self, snapshot, *, ttl_seconds):
            from datetime import timedelta
            from src.domain.search_api import SearchSeed

            trace.append("seed")
            saved_snapshots.append(snapshot)
            now = SystemClock().now()
            return SearchSeed(
                search_id="srch_test",
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
                evidence_count=len(snapshot.evidence),
                seed_snapshot_hash="sha256:test",
            )

    service = SearchService(
        discovery=Discovery(),
        evidence_assembler=Evidence(),
        trust_annotator=Trust(),
        answerability=Policy(),
        seed_store=SeedStore(),
        clock=SystemClock(),
        deadline_ms=30000,
        seed_ttl_seconds=60,
    )
    response = service.execute(SearchCommand("q"))

    assert trace == ["discovery", "evidence", "trust", "answer", "seed"]
    assert [failure.code for failure in response.failures] == [
        "PLAN", "RECALL", "RANK"
    ]
    assert saved_snapshots[0].requested_source_types is None
    assert saved_snapshots[0].planned_source_types == ["web", "academic"]


def test_engine_is_only_a_compatibility_facade():
    source = (Path(__file__).resolve().parents[1] / "src" / "engine.py").read_text()

    for forbidden in (
        "import requests",
        "ThreadPoolExecutor",
        "src.providers",
        "src.pipeline.rerank",
        "plan_query(",
        "annotate_evidence(",
    ):
        assert forbidden not in source
    assert len(source.splitlines()) < 220
