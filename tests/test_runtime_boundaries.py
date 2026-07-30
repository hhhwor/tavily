from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from src.application.commands import SearchCommand
from src.application.outcomes import PlannedQuery, RecallOutcome
from src.application.ports.runtime import Deadline
from src.application.ports.retrieval import RetrievalRequest
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.source_registry import SourceRegistry
from src.config import Settings
from src.infrastructure.cache import InMemoryCache
from src.infrastructure.query_rewriter import SiliconFlowQueryRewriter
from src.models import SearchPlan
from src.domain.documents import RetrievedDocument
from src.domain.search import SearchResult
from src.providers.base import SearchProvider
from src.ranking.ports import NoOpReranker, Reranker
from src.ranking.adapters.siliconflow import SiliconFlowReranker
from src.application.ports.retrieval import SourceDescriptor


ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> datetime:
        return datetime(2026, 7, 17, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value


def test_memory_cache_uses_injected_monotonic_clock():
    clock = FakeClock()
    cache = InMemoryCache(2, monotonic=clock.monotonic)

    cache.set("key", "value", ttl=5)
    clock.value = 4.9
    assert cache.get("key") == "value"
    clock.value = 5.0
    assert cache.get("key") is None


def test_query_rewriter_owns_thread_safe_cache_outside_l0():
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "vector database"}}]}

    class Session:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Response()

    rewriter = SiliconFlowQueryRewriter(
        "secret",
        "https://example.test/v1",
        "model",
        cache=InMemoryCache(),
        http_session=Session(),
    )

    assert rewriter.rewrite("what is a vector database") == "vector database"
    assert rewriter.rewrite("what is a vector database") == "vector database"
    assert len(calls) == 1


def test_scorer_cache_creates_one_instance_under_concurrency():
    clock = FakeClock()
    calls = 0
    lock = Lock()

    def factory(*args):
        nonlocal calls
        time.sleep(0.01)
        with lock:
            calls += 1
        return NoOpReranker()

    with ThreadPoolExecutor(max_workers=8) as executor:
        service = RankingService(
            Settings(openalex_enabled=False, patent_es_enabled=False),
            NoOpReranker(),
            factory,
            executor,
            clock=clock,
        )
        scorers = list(executor.map(
            lambda _: service._select_text_scorer(True, "custom", "model"),
            range(20),
        ))
        service.close()

    assert calls == 1
    assert all(scorer is scorers[0] for scorer in scorers)


def test_expired_deadline_cancels_pending_retrieval():
    class PendingExecutor:
        def submit(self, function, *args, **kwargs):
            return Future()

    class Source(SearchProvider):
        descriptor = SourceDescriptor(id="pending", kind="web")

        def search(self, query, top_k=10, recency=None):
            raise AssertionError("pending future must not run")

    clock = FakeClock()
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        cache_enabled=False,
    )
    coordinator = RecallCoordinator(
        settings,
        SourceRegistry([Source()]),
        None,
        PendingExecutor(),
        clock=clock.now,
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="q",
            normalized_query="q",
            providers=["pending"],
        ),
        search_query="q",
        academic_query="q",
        active_provider_names=("pending",),
    )

    outcome = coordinator.recall(
        planned, deadline=Deadline.after(0, clock)
    )

    assert outcome.web == ()
    assert outcome.failures[0].code == "SEARCH_DEADLINE_EXCEEDED"


def test_recall_binds_provider_request_timeout_to_remaining_deadline():
    received = []

    class Source(SearchProvider):
        descriptor = SourceDescriptor(id="bounded", kind="web")

        def search(self, query, top_k=10, recency=None):
            return []

        def retrieve(self, request: RetrievalRequest):
            received.append(request)
            return super().retrieve(request)

    clock = FakeClock()
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        cache_enabled=False,
        provider_timeout=15,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        coordinator = RecallCoordinator(
            settings,
            SourceRegistry([Source()]),
            None,
            executor,
            clock=clock.now,
        )
        planned = PlannedQuery(
            plan=SearchPlan(
                raw_query="q",
                normalized_query="q",
                providers=["bounded"],
            ),
            search_query="q",
            academic_query="q",
            active_provider_names=("bounded",),
        )
        outcome = coordinator.recall(
            planned, deadline=Deadline.after(2_000, clock)
        )

    assert outcome.failures == ()
    assert len(received) == 1
    assert received[0].timeout_seconds == 2.0


def test_siliconflow_http_total_timeout_is_capped_by_request_budget():
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.5}]}

    class Session:
        def post(self, *args, **kwargs):
            calls.append(kwargs)
            return Response()

    scorer = SiliconFlowReranker(
        "secret",
        timeout=30,
        http_session=Session(),
    )

    assert scorer.score_with_timeout(
        "query", ["document"], timeout_seconds=1.25
    ) == [0.5]
    assert calls[0]["timeout"].total == 1.25


def test_ranking_binds_remaining_deadline_to_timeout_aware_scorer():
    timeouts = []

    class DeadlineAwareScorer(Reranker):
        name = "deadline-aware"

        def rerank(self, query, results, top_k):
            return results[:top_k]

        def score(self, query, texts):
            raise AssertionError("deadline-aware path must be used")

        def score_with_timeout(self, query, texts, *, timeout_seconds=None):
            timeouts.append(timeout_seconds)
            return [0.5 for _ in texts]

    clock = FakeClock()
    scorer = DeadlineAwareScorer()
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="quality",
    )
    result = SearchResult(
        url="https://example.test",
        title="Example",
        content="Document",
        source="test",
    )
    recalled = RecallOutcome(
        web=(RetrievedDocument.from_result(result, "web"),),
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="q",
            normalized_query="q",
            providers=["test"],
            top_k=1,
        ),
        search_query="q",
        academic_query="q",
        active_provider_names=("test",),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        service = RankingService(
            settings,
            scorer,
            lambda *args: scorer,
            executor,
            clock=clock,
        )
        outcome = service.rank(
            SearchCommand(query="q", limit=1),
            planned,
            recalled,
            deadline=Deadline.after(1_500, clock),
        )
        service.close()

    assert outcome.failures == ()
    assert len(outcome.web) == 1
    assert timeouts == [1.5]


def test_application_modules_depend_on_runtime_ports_not_system_clients():
    for relative_path in (
        "src/l0.py",
        "src/application/query_planner.py",
        "src/application/search_service.py",
        "src/application/recall.py",
    ):
        source = (ROOT / relative_path).read_text()
        assert "import requests" not in source
        assert "time.time(" not in source
        assert "datetime.now(" not in source
