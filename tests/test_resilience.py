from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import Future

import pytest

from src.application.commands import SearchCommand
from src.application.degradation import degradation_for
from src.application.failures import search_failure
from src.application.outcomes import PlannedQuery, RecallOutcome
from src.application.ports.runtime import Deadline
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.search_service import SearchService
from src.application.source_registry import SourceRegistry
from src.config import Settings
from src.domain.errors import ExternalServiceError
from src.domain.documents import RetrievedDocument
from src.domain.search import SearchPlan, SearchResult
from src.infrastructure.resilience import CircuitOpenError, ResilienceManager
from src.providers.base import SearchProvider
from src.ranking.ports import Reranker
from src.application.ports.retrieval import SourceDescriptor


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def now(self) -> datetime:
        return datetime(2026, 7, 27, tzinfo=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.value += seconds


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def _settings(**overrides) -> Settings:
    values = {
        "openalex_enabled": False,
        "patent_es_enabled": False,
        "resilience_max_attempts": 2,
        "resilience_backoff_base_ms": 100,
        "resilience_backoff_max_ms": 1000,
        "circuit_failure_threshold": 3,
        "circuit_open_seconds": 10,
    }
    values.update(overrides)
    return Settings(**values)


def _transient(
    code: str = "SEARCH_UPSTREAM_UNAVAILABLE",
    *,
    retry_after_seconds: float | None = None,
) -> ExternalServiceError:
    return ExternalServiceError(
        provider="provider",
        code=code,
        recoverable=True,
        retry_after_seconds=retry_after_seconds,
    )


def test_recoverable_failure_retries_then_closes_circuit_on_success():
    clock = FakeClock()
    sleeps = []
    calls = 0
    manager = ResilienceManager(
        _settings(),
        clock,
        sleeper=lambda seconds: (sleeps.append(seconds), clock.advance(seconds)),
        random_value=lambda: 0.0,
    )

    def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _transient()
        return "ok"

    assert manager.call("provider", "search", operation) == "ok"
    assert calls == 2
    assert sleeps == [0.05]
    assert manager.snapshot()["dependencies"]["provider"] == {
        "state": "closed",
        "consecutive_failures": 0,
        "retry_after_seconds": 0.0,
        "calls": 1,
        "retries": 1,
        "successes": 1,
    }


def test_nonrecoverable_failure_is_not_retried_or_counted_by_circuit():
    clock = FakeClock()
    calls = 0
    manager = ResilienceManager(
        _settings(),
        clock,
        sleeper=lambda seconds: pytest.fail("must not sleep"),
    )
    error = ExternalServiceError(
        provider="provider",
        code="SEARCH_AUTH_FAILED",
        recoverable=False,
    )

    def operation():
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(ExternalServiceError) as caught:
        manager.call("provider", "search", operation)

    assert caught.value is error
    assert calls == 1
    state = manager.snapshot()["dependencies"]["provider"]
    assert state["state"] == "closed"
    assert state["consecutive_failures"] == 0
    assert state["failures"] == 1
    assert "retries" not in state


def test_circuit_opens_and_half_open_probe_recovers_dependency():
    clock = FakeClock()
    calls = 0
    manager = ResilienceManager(
        _settings(
            resilience_max_attempts=1,
            circuit_failure_threshold=2,
            circuit_open_seconds=10,
        ),
        clock,
        sleeper=lambda seconds: pytest.fail("must not sleep"),
    )

    def failing():
        nonlocal calls
        calls += 1
        raise _transient()

    for _ in range(2):
        with pytest.raises(ExternalServiceError):
            manager.call("provider", "search", failing)

    with pytest.raises(CircuitOpenError) as caught:
        manager.call("provider", "search", failing)
    assert calls == 2
    assert caught.value.retry_after_seconds == 10

    clock.advance(10)
    assert manager.call("provider", "search", lambda: "recovered") == "recovered"
    state = manager.snapshot()["dependencies"]["provider"]
    assert state["state"] == "closed"
    assert state["consecutive_failures"] == 0
    assert state["circuit_rejections"] == 1


def test_retry_backoff_never_crosses_request_deadline():
    clock = FakeClock()
    calls = 0
    manager = ResilienceManager(
        _settings(resilience_backoff_base_ms=100),
        clock,
        sleeper=lambda seconds: pytest.fail("must not sleep"),
        random_value=lambda: 0.0,
    )

    def operation():
        nonlocal calls
        calls += 1
        raise _transient()

    with pytest.raises(ExternalServiceError):
        manager.call(
            "provider",
            "search",
            operation,
            deadline=Deadline.after(40, clock),
        )

    assert calls == 1


def test_retry_after_is_preserved_in_public_failure():
    error = CircuitOpenError("provider", retry_after_seconds=1.25)
    failure = search_failure(
        stage="provider_search",
        source="provider",
        source_type="web",
        code="PROVIDER_SEARCH_FAILED",
        message=error,
    )

    assert failure.code == "CIRCUIT_OPEN"
    assert failure.retry_after_ms == 1250
    public = SearchService._failure(failure)
    assert public.retry_after_ms == 1250
    assert public.degradation.action == "continue_available_sources"
    decision = degradation_for(
        stage=failure.stage,
        code=failure.code,
        retryable=failure.recoverable,
    )
    assert decision.model_dump() == {
        "action": "continue_available_sources",
        "impact": "coverage",
        "retry_owner": "server",
    }


def test_recall_uses_shared_resilience_policy_before_returning_failure():
    clock = FakeClock()
    manager = ResilienceManager(
        _settings(),
        clock,
        sleeper=lambda seconds: clock.advance(seconds),
        random_value=lambda: 0.0,
    )

    class Provider(SearchProvider):
        descriptor = SourceDescriptor(id="provider", kind="web")

        def __init__(self):
            self.calls = 0

        def search(self, query, top_k=10, recency=None):
            self.calls += 1
            if self.calls == 1:
                raise _transient()
            return [
                SearchResult(
                    url="https://example.test",
                    title="recovered",
                    source="provider",
                )
            ]

    provider = Provider()
    coordinator = RecallCoordinator(
        _settings(cache_enabled=False),
        SourceRegistry([provider]),
        None,
        InlineExecutor(),
        clock=clock.now,
        resilience=manager,
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="q",
            normalized_query="q",
            providers=["provider"],
        ),
        search_query="q",
        academic_query="q",
        active_provider_names=("provider",),
    )

    outcome = coordinator.recall(planned)

    assert provider.calls == 2
    assert len(outcome.web) == 1
    assert outcome.failures == ()


def test_query_rewrite_uses_resilience_policy_and_keeps_successful_retry():
    clock = FakeClock()
    manager = ResilienceManager(
        _settings(),
        clock,
        sleeper=lambda seconds: clock.advance(seconds),
        random_value=lambda: 0.0,
    )

    class Rewriter:
        def __init__(self):
            self.calls = 0

        def rewrite_with_timeout(
            self,
            query,
            *,
            academic=False,
            timeout_seconds=None,
        ):
            self.calls += 1
            if self.calls == 1:
                raise _transient("QUERY_REWRITE_TIMEOUT")
            return "rewritten query"

    rewriter = Rewriter()
    planner = QueryPlanner(
        _settings(
            rewrite_enabled=True,
            siliconflow_api_key="token",
            openalex_academic_detect=False,
            patent_detect=False,
        ),
        rewriter,
        resilience=manager,
    )

    planned = planner.plan(
        SearchCommand("original query"),
        ["provider"],
        academic_available=False,
        patent_available=False,
    )

    assert rewriter.calls == 2
    assert planned.search_query == "rewritten query"
    assert planned.failures == ()


def test_ranking_uses_resilience_policy_before_source_order_fallback():
    clock = FakeClock()
    manager = ResilienceManager(
        _settings(),
        clock,
        sleeper=lambda seconds: clock.advance(seconds),
        random_value=lambda: 0.0,
    )

    class Scorer(Reranker):
        name = "scorer"

        def __init__(self):
            self.calls = 0

        def rerank(self, query, results, top_k):
            return results[:top_k]

        def score(self, query, texts):
            raise AssertionError("score_with_timeout must be used")

        def score_with_timeout(self, query, texts, *, timeout_seconds=None):
            self.calls += 1
            if self.calls == 1:
                raise _transient("RERANK_TIMEOUT")
            return [0.5 for _ in texts]

    scorer = Scorer()
    settings = _settings(ranking_profile="quality")
    service = RankingService(
        settings,
        scorer,
        lambda *args: scorer,
        InlineExecutor(),
        clock=clock,
        resilience=manager,
    )
    result = SearchResult(
        url="https://example.test",
        title="document",
        source="provider",
    )
    recalled = RecallOutcome(
        web=(RetrievedDocument.from_result(result, "web"),),
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="q",
            normalized_query="q",
            providers=["provider"],
            top_k=1,
        ),
        search_query="q",
        academic_query="q",
        active_provider_names=("provider",),
    )

    outcome = service.rank(
        SearchCommand("q", limit=1),
        planned,
        recalled,
    )

    assert scorer.calls == 2
    assert len(outcome.web) == 1
    assert outcome.failures == ()


@pytest.mark.parametrize(
    ("stage", "code", "retryable", "expected"),
    [
        (
            "query_rewrite",
            "QUERY_REWRITE_FAILED",
            True,
            ("use_original_query", "quality", "server"),
        ),
        (
            "rerank",
            "RERANK_TIMEOUT",
            True,
            ("use_unreranked_results", "quality", "server"),
        ),
        (
            "seed_store",
            "SEARCH_SEED_UNAVAILABLE",
            True,
            ("omit_research_seed", "feature", "caller"),
        ),
        (
            "provider_search",
            "SEARCH_DEADLINE_EXCEEDED",
            True,
            ("continue_available_sources", "coverage", "caller"),
        ),
        (
            "routing",
            "PROVIDER_UNAVAILABLE",
            False,
            ("continue_available_sources", "coverage", "none"),
        ),
    ],
)
def test_degradation_matrix(stage, code, retryable, expected):
    detail = degradation_for(
        stage=stage,
        code=code,
        retryable=retryable,
    )
    assert (detail.action, detail.impact, detail.retry_owner) == expected
