from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import _translate_error, create_app
from src.application.commands import ResearchCommand, SearchCommand
from src.application.model_router import PrivacyAwareModelRouter
from src.application.outcomes import PlannedQuery, RecallOutcome
from src.application.ports.runtime import Deadline, DeadlineExceededError
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.research_dispatcher import (
    ResearchDispatcher,
    ResearchQueueFull,
)
from src.application.research_execution import (
    BudgetExceededError,
    BudgetLedger,
    CancellationToken,
    ResearchCancelledError,
)
from src.application.research_scope import exclusion_reason, validate_scope
from src.application.research_service import ResearchRequestError, ResearchService
from src.application.verify_service import VerifyService
from src.config import Settings
from src.bootstrap import build_container
from src.infrastructure.cache import InMemoryCache
from src.domain.documents import RetrievedDocument
from src.domain.evidence import (
    Evidence,
    EvidenceAccess,
    EvidencePassage,
    EvidencePatent,
    EvidenceProvenance,
)
from src.domain.research import (
    ResearchPrivacy,
    ResearchScope,
    ResearchTimeScope,
)
from src.domain.search import SearchPlan, SearchResult
from src.domain.search_api import (
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchQuery,
    SearchSeedSnapshot,
)
from src.domain.trust import CandidateClaim
from src.application.source_registry import SourceRegistry
from src.application.ports.retrieval import SourceDescriptor
from src.providers.base import SearchProvider
from src.ranking.ports import Reranker
from src.trust import ClaimVerifier, annotate_evidence


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> datetime:
        return datetime(2026, 8, 3, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value


def _evidence(
    *,
    kind: str = "web",
    published: str = "2025-01-02",
    updated: str | None = "2025-02-03",
    language: str | None = "zh",
    license_id: str | None = "cc-by",
    country: str = "CN",
    application_date: str = "2024-01-01",
    publication_date: str = "2025-01-02",
    ipc: str = "H01M10/00",
) -> Evidence:
    return Evidence(
        id=f"{kind}:1",
        result_id=f"{kind}:1",
        type=kind,
        title="测试证据",
        url="https://example.test/evidence",
        published_date=published,
        updated_date=updated,
        language=language,
        passage=EvidencePassage(text="测试陈述"),
        access=EvidenceAccess(license=license_id),
        provenance=EvidenceProvenance(
            canonical_url="https://example.test/evidence",
            retrieved_at="2026-08-03T00:00:00Z",
            license=license_id,
        ),
        patent=(
            EvidencePatent(
                country=country,
                application_date=application_date,
                publication_date=publication_date,
                ipc_main=ipc,
            )
            if kind == "patent"
            else None
        ),
    )


def _seed(source_types=None) -> SearchSeedSnapshot:
    return SearchSeedSnapshot(
        requested_source_types=source_types,
        planned_source_types=list(source_types or []),
        query=SearchQuery(
            original="测试问题",
            effective="测试问题",
            filters_requested=RequestedFilters(),
        ),
        evidence=[],
        retrieval_assessment=RetrievalAssessment(),
        retrieval_boundary=RetrievalBoundary(
            query_time=datetime.now(timezone.utc),
            deadline_ms=30_000,
        ),
    )


def test_budget_ledger_reserves_commits_and_releases_atomically():
    clock = FakeClock()
    ledger = BudgetLedger({"candidates": 3}, monotonic=clock.monotonic)

    reservation = ledger.reserve("candidates", 2)
    assert ledger.remaining("candidates") == 1
    with pytest.raises(BudgetExceededError):
        ledger.reserve("candidates", 2)
    reservation.commit(actual=1)
    assert ledger.used("candidates") == 1
    assert ledger.remaining("candidates") == 2

    released = ledger.reserve("candidates", 2)
    released.release()
    assert ledger.remaining("candidates") == 2
    clock.value = 0.125
    assert ledger.snapshot()["elapsed_ms"] == 125


def test_cancellation_token_combines_local_and_durable_signals():
    durable = {"cancelled": False}
    token = CancellationToken(lambda: durable["cancelled"])
    token.raise_if_cancelled()
    durable["cancelled"] = True
    with pytest.raises(ResearchCancelledError):
        token.raise_if_cancelled()

    local = CancellationToken()
    local.cancel()
    assert local.cancelled is True


def test_scope_gate_enforces_every_supported_field_and_time_basis():
    patent = _evidence(kind="patent")
    scope = ResearchScope(
        source_types=["patent"],
        time=ResearchTimeScope(
            **{"from": date(2023, 1, 1), "to": date(2024, 12, 31)},
            basis="filing",
        ),
        languages=["zh"],
        jurisdictions=["CN"],
        licenses=["cc-by"],
        required_classifications=["H01M"],
    )
    validate_scope(scope)
    assert exclusion_reason(patent, scope) is None
    assert exclusion_reason(_evidence(kind="web"), scope) == (
        "SOURCE_TYPE_OUT_OF_SCOPE"
    )
    assert exclusion_reason(
        patent.model_copy(update={"language": "en"}), scope
    ) == "LANGUAGE_OUT_OF_SCOPE"
    assert exclusion_reason(
        patent.model_copy(update={"access": EvidenceAccess(license="closed")}),
        scope,
    ) == "LICENSE_OUT_OF_SCOPE"
    assert exclusion_reason(
        patent.model_copy(update={
            "patent": patent.patent.model_copy(update={"country": "US"})
        }),
        scope,
    ) == "JURISDICTION_OUT_OF_SCOPE"
    assert exclusion_reason(
        patent.model_copy(update={
            "patent": patent.patent.model_copy(update={"ipc_main": "C07D"})
        }),
        scope,
    ) == "CLASSIFICATION_OUT_OF_SCOPE"
    assert exclusion_reason(
        patent.model_copy(update={
            "patent": patent.patent.model_copy(
                update={"application_date": "2025-01-01"}
            )
        }),
        scope,
    ) == "DATE_OUT_OF_SCOPE"

    with pytest.raises(ValueError, match="RESEARCH_SCOPE_UNSUPPORTED"):
        validate_scope(ResearchScope(time=ResearchTimeScope(
            **{"from": date(2020, 1, 1)}, basis="priority"
        )))


@pytest.mark.parametrize(
    ("basis", "scope_from", "scope_to", "expected"),
    [
        ("published", date(2025, 1, 1), date(2025, 1, 31), None),
        ("publication", date(2025, 1, 1), date(2025, 1, 31), None),
        ("updated", date(2025, 2, 1), date(2025, 2, 28), None),
        (
            "updated",
            date(2025, 1, 1),
            date(2025, 1, 31),
            "DATE_OUT_OF_SCOPE",
        ),
    ],
)
def test_supported_time_bases_are_behavioral(
    basis,
    scope_from,
    scope_to,
    expected,
):
    scope = ResearchScope(time=ResearchTimeScope(
        **{"from": scope_from, "to": scope_to},
        basis=basis,
    ))
    assert exclusion_reason(_evidence(kind="patent"), scope) == expected


def test_non_published_time_basis_is_not_misapplied_as_provider_date_filter():
    scope = ResearchScope(time=ResearchTimeScope(
        **{"from": date(2024, 1, 1), "to": date(2024, 12, 31)},
        basis="filing",
    ))
    filters = ResearchService._search_filters(scope)
    assert filters.published_from is None
    assert filters.published_to is None


def test_policy_scope_mismatch_is_rejected_during_resolution():
    service = ResearchService(
        seed_store=None,
        task_store=None,
        discovery=None,
        evidence_assembler=None,
        trust_annotator=None,
        pdf_gateway=None,
        verify_service=None,
        clock=FakeClock(),
    )

    with pytest.raises(ResearchRequestError) as caught:
        service._resolve(
            ResearchCommand(
                search_id="srch_test",
                profile="literature_review",
                scope=ResearchScope(source_types=["web"]),
            ),
            SimpleNamespace(snapshot=_seed(["web"])),
        )

    assert caught.value.code == "RESEARCH_POLICY_UNSATISFIABLE"


def test_restricted_route_rejects_creation_when_no_local_verify_path():
    service = ResearchService(
        seed_store=None,
        task_store=None,
        discovery=None,
        evidence_assembler=None,
        trust_annotator=None,
        pdf_gateway=None,
        verify_service=None,
        clock=FakeClock(),
        model_router=PrivacyAwareModelRouter(
            local_verification_available=False
        ),
    )

    with pytest.raises(ResearchRequestError) as caught:
        service._resolve(
            ResearchCommand(
                search_id="srch_test",
                privacy=ResearchPrivacy(mode="restricted"),
            ),
            SimpleNamespace(snapshot=_seed(["web"])),
        )

    assert caught.value.code == "PRIVACY_POLICY_UNSATISFIABLE"


def test_restricted_route_makes_external_model_spy_call_count_zero():
    calls = {"rewrite": 0, "rerank": 0, "verify": 0}
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        rewrite_enabled=True,
        openalex_query_rewrite=True,
        siliconflow_api_key="test-key",
        ranking_profile="quality",
    )

    class Rewriter:
        def rewrite(self, query, *, academic=False):
            calls["rewrite"] += 1
            return "rewritten"

    planner = QueryPlanner(settings, Rewriter())
    planned = planner.plan(
        SearchCommand("测试", source_types=("web",)),
        ["web-provider"],
        academic_available=False,
        patent_available=False,
        allow_external_models=False,
    )
    assert planned.search_query == "测试"

    class ExternalScorer(Reranker):
        name = "external-spy"
        is_external = True

        def rerank(self, query, results, top_k):
            calls["rerank"] += 1
            return results[:top_k]

        def score(self, query, texts):
            calls["rerank"] += 1
            return [0.5 for _ in texts]

    result = SearchResult(
        url="https://example.test",
        title="测试",
        content="测试陈述",
    )
    recalled = RecallOutcome(
        web=(RetrievedDocument.from_result(result, "web"),)
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        ranking = RankingService(
            settings,
            ExternalScorer(),
            lambda *args: ExternalScorer(),
            executor,
            clock=FakeClock(),
        )
        ranking.rank(
            SearchCommand("测试", limit=1),
            planned,
            recalled,
            allow_external_models=False,
        )
        ranking.close()

    class ExternalClassifier:
        name = "external-verify-spy"
        is_external = True

        def classify_pairs(self, pairs):
            calls["verify"] += 1
            return {}

    evidence = annotate_evidence([_evidence()])[0]
    VerifyService(ClaimVerifier(ExternalClassifier())).verify(
        "测试",
        [CandidateClaim(id="c1", text="测试陈述")],
        [evidence],
        use_external_models=False,
    )

    assert calls == {"rewrite": 0, "rerank": 0, "verify": 0}


def test_verify_model_retry_is_bounded_by_research_deadline():
    import requests
    from src.trust.entailment import SiliconFlowEntailmentClassifier

    clock = FakeClock()
    timeouts = []

    class Session:
        def post(self, *args, **kwargs):
            timeouts.append(kwargs["timeout"].total)
            clock.value = 0.2
            raise requests.Timeout("slow model")

    verifier = ClaimVerifier(SiliconFlowEntailmentClassifier(
        "token",
        "https://example.test/v1",
        "model",
        timeout=10,
        http_session=Session(),
    ))
    evidence = annotate_evidence([_evidence()])[0]

    with pytest.raises(DeadlineExceededError):
        verifier.verify(
            query="测试",
            claims=[CandidateClaim(id="c1", text="测试陈述")],
            evidence=[evidence],
            deadline=Deadline.after(100, clock),
        )

    assert timeouts == [0.1]


def test_dispatcher_has_bounded_admission_ttl_stats_and_429_mapping():
    clock = FakeClock()
    started = Event()
    release = Event()
    expired = []

    def runner(research_id):
        if research_id == "first":
            started.set()
            release.wait(timeout=2)

    dispatcher = ResearchDispatcher(
        runner,
        max_workers=1,
        queue_capacity=1,
        queue_ttl_ms=10,
        retry_after_seconds=3,
        on_expired=lambda research_id, age: expired.append((research_id, age)),
        monotonic=clock.monotonic,
    )
    try:
        dispatcher.submit("first")
        assert started.wait(timeout=1)
        dispatcher.submit("second")
        with pytest.raises(ResearchQueueFull) as caught:
            dispatcher.submit("third")
        translated = _translate_error(caught.value)
        assert translated.status_code == 429
        assert translated.headers == {"Retry-After": "3"}
        assert translated.detail["code"] == "RESEARCH_QUEUE_FULL"

        clock.value = 0.02
        release.set()
        for _ in range(100):
            if expired:
                break
            time.sleep(0.005)
        assert expired == [("second", 20)]
        stats = dispatcher.stats()
        assert stats["accepted_total"] == 2
        assert stats["rejected_total"] == 1
        assert stats["expired_total"] == 1
        assert stats["active"] == 0
        assert stats["queued"] == 0
    finally:
        release.set()
        dispatcher.close()


def test_research_workload_uses_dedicated_recall_and_ranking_pools():
    class ImmediateExecutor:
        def __init__(self):
            self.calls = 0

        def submit(self, function, *args, **kwargs):
            self.calls += 1
            future = Future()
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future

    class Source(SearchProvider):
        descriptor = SourceDescriptor(id="test", kind="web")

        def search(self, query, top_k=10, recency=None):
            return [SearchResult(
                url="https://example.test",
                title="测试",
                content="测试陈述",
                source="test",
            )]

    search_recall = ImmediateExecutor()
    research_recall = ImmediateExecutor()
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        cache_enabled=False,
        ranking_profile="fast",
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="测试",
            normalized_query="测试",
            providers=("test",),
            top_k=1,
        ),
        search_query="测试",
        academic_query="测试",
        active_provider_names=("test",),
    )
    recall = RecallCoordinator(
        settings,
        SourceRegistry([Source()]),
        None,
        search_recall,
        research_executor=research_recall,
        clock=FakeClock().now,
    )
    recalled = recall.recall(planned, workload_class="research")
    assert research_recall.calls == 1
    assert search_recall.calls == 0

    search_ranking = ImmediateExecutor()
    research_ranking = ImmediateExecutor()
    ranking = RankingService(
        settings,
        ExternalScorerForPoolTest(),
        lambda *args: ExternalScorerForPoolTest(),
        search_ranking,
        research_executor=research_ranking,
        clock=FakeClock(),
    )
    ranking.rank(
        SearchCommand("测试", limit=1),
        planned,
        recalled,
        deadline=Deadline.after(1_000, FakeClock()),
        workload_class="research",
    )
    assert research_ranking.calls == 1
    assert search_ranking.calls == 0


def test_restricted_recall_does_not_read_or_write_shared_cache():
    calls = {"provider": 0}

    class ImmediateExecutor:
        def submit(self, function, *args, **kwargs):
            future = Future()
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future

    class Source(SearchProvider):
        descriptor = SourceDescriptor(id="test", kind="web")

        def search(self, query, top_k=10, recency=None):
            calls["provider"] += 1
            return []

    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        cache_enabled=True,
    )
    coordinator = RecallCoordinator(
        settings,
        SourceRegistry([Source()]),
        InMemoryCache(),
        ImmediateExecutor(),
        clock=FakeClock().now,
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="测试",
            normalized_query="测试",
            providers=("test",),
        ),
        search_query="测试",
        academic_query="测试",
        active_provider_names=("test",),
    )

    coordinator.recall(planned, allow_shared_cache=False)
    coordinator.recall(planned, allow_shared_cache=False)

    assert calls["provider"] == 2


def test_quick_standard_and_deep_finish_within_deadline_plus_grace(tmp_path):
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
        mcp_mode="false",
        state_db_path=str(tmp_path / "state.sqlite3"),
        research_max_workers=1,
    )
    container = build_container(settings, include_mcp=False)
    with TestClient(create_app(container)) as client:
        search = client.post("/search", json={
            "query": "M0 deadline test",
            "source_types": ["web"],
        })
        search_id = search.json()["research_seed"]["search_id"]
        for index, depth in enumerate(("quick", "standard", "deep")):
            started_at = time.monotonic()
            started = client.post(
                "/research",
                headers={"Idempotency-Key": f"deadline-{index}"},
                json={
                    "search_id": search_id,
                    "depth": depth,
                    "budget": {"deadline_ms": 1000},
                },
            )
            assert started.status_code == 202
            research_id = started.json()["research_id"]
            for _ in range(200):
                task = client.get(f"/research/{research_id}").json()
                if task["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.005)
            elapsed = time.monotonic() - started_at
            assert task["state"] in {"completed", "partial", "failed"}
            assert elapsed <= 1.5


def test_research_endpoint_returns_stable_429_when_queue_is_full(tmp_path):
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
        mcp_mode="false",
        state_db_path=str(tmp_path / "state.sqlite3"),
        research_max_workers=1,
        research_queue_capacity=0,
        research_queue_retry_after_seconds=2,
    )
    container = build_container(settings, include_mcp=False)
    original_runner = container.research_dispatcher._runner
    started_event = Event()
    release_event = Event()

    def blocking_runner(research_id):
        started_event.set()
        release_event.wait(timeout=2)
        original_runner(research_id)

    container.research_dispatcher._runner = blocking_runner
    try:
        with TestClient(create_app(container)) as client:
            search = client.post("/search", json={
                "query": "queue overload",
                "source_types": ["web"],
            })
            search_id = search.json()["research_seed"]["search_id"]
            first = client.post(
                "/research",
                headers={"Idempotency-Key": "queue-first"},
                json={"search_id": search_id, "depth": "quick"},
            )
            assert first.status_code == 202
            assert started_event.wait(timeout=1)

            overloaded = client.post(
                "/research",
                headers={"Idempotency-Key": "queue-second"},
                json={"search_id": search_id, "depth": "quick"},
            )
            assert overloaded.status_code == 429
            assert overloaded.headers["retry-after"] == "2"
            assert overloaded.json()["detail"]["code"] == (
                "RESEARCH_QUEUE_FULL"
            )
            release_event.set()
    finally:
        release_event.set()


class ExternalScorerForPoolTest(Reranker):
    name = "pool-test"

    def rerank(self, query, results, top_k):
        return results[:top_k]

    def score(self, query, texts):
        return [0.5 for _ in texts]
