from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.application.citation_audit import CitationCoverageAuditor
from src.application.ports.model_router import ResolvedModelRoute
from src.application.ports.runtime import Deadline
from src.application.research_artifacts import (
    ResearchArtifactExpired,
    ResearchArtifactRenderer,
    ResearchArtifactService,
)
from src.application.research_dossier_builder import StructuredDossierBuilder
from src.application.research_execution import (
    BudgetLedger,
    CancellationToken,
    ExecutionContext,
)
from src.application.research_synthesis import ResearchSynthesizer
from src.application.research_runner import ResearchRunner
from src.bootstrap import build_container
from src.config import Settings
from src.domain.evidence import (
    Evidence,
    EvidenceLocator,
    EvidencePassage,
    SearchBoundary,
)
from src.domain.research import (
    AssessmentDimension,
    EvidenceFunnel,
    ResearchAssessment,
    ResearchBudget,
    ResearchCoverage,
    ResearchDossier,
    ResearchLinks,
    ResearchObjective,
    ResearchPrivacy,
    ResearchScope,
    ResearchStatement,
    ResearchSynthesisSnapshot,
    ResearchTaskEnvelope,
    ResolvedResearch,
)
from src.domain.search_api import (
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchQuery,
    SearchSeedSnapshot,
)
from src.domain.synthesis import (
    SynthesisDraft,
    SynthesisGatewayResult,
    SynthesisRequest,
)
from src.domain.trust import (
    CandidateClaim,
    ClaimAssessment,
    ClaimEvidenceRelation,
)
from src.infrastructure.sqlite_research_store import SqliteResearchStore
from src.infrastructure.siliconflow_synthesis import (
    SiliconFlowSynthesisGateway,
)


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 4, tzinfo=timezone.utc)
        self.tick = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.tick


def _dimension(status: str = "sufficient") -> AssessmentDimension:
    return AssessmentDimension(status=status)


def _resolved() -> ResolvedResearch:
    return ResolvedResearch(
        objective=ResearchObjective(question="关键陈述是否成立"),
        scope=ResearchScope(source_types=["academic"]),
        profile="technology_landscape",
        depth="quick",
        policy_id="technical-landscape.v1",
        policy_version="1",
        budget=ResearchBudget(
            max_rounds=1,
            max_candidates=10,
            max_deep_reads=2,
            deadline_ms=30_000,
        ),
        privacy=ResearchPrivacy(),
    )


def _dossier(*, conflicted: bool = False) -> ResearchDossier:
    claim = CandidateClaim(id="claim_1", text="关键陈述成立")
    evidence: dict[str, Evidence] = {}
    relations: list[ClaimEvidenceRelation] = []
    values = [("evidence_support", "supports", "关键陈述成立")]
    if conflicted:
        values.append(("evidence_conflict", "contradicts", "关键陈述不成立"))
    for evidence_id, relation, quote in values:
        locator = EvidenceLocator(
            document_id=f"doc_{evidence_id}",
            version_id=f"version_{evidence_id}",
            chunk_index=0,
            char_start=0,
            char_end=len(quote),
        )
        evidence[evidence_id] = Evidence(
            id=evidence_id,
            result_id=evidence_id,
            type="academic",
            title=evidence_id,
            passage=EvidencePassage(text=quote),
            locator=locator,
        )
        relations.append(ClaimEvidenceRelation(
            evidence_id=evidence_id,
            relation=relation,
            confidence="high",
            quote=quote,
            locator=locator,
            evidence_quality="citable",
            qualified=True,
        ))
    assessment = ClaimAssessment(
        claim=claim,
        status="conflicted" if conflicted else "supported",
        relations=relations,
        support_refs=["evidence_support"],
        conflict_refs=["evidence_conflict"] if conflicted else [],
        independent_support_count=1,
    )
    return ResearchDossier(
        findings=[{"claim": claim, "assessment": assessment}],
        assessment=ResearchAssessment(
            overall="conflicted" if conflicted else "sufficient",
            coverage=_dimension(),
            independence=_dimension(),
            locatability=_dimension(),
            consistency=_dimension("conflicted" if conflicted else "sufficient"),
            source_quality=_dimension(),
            reproducibility=_dimension(),
        ),
        evidence_funnel=EvidenceFunnel(adopted=len(evidence)),
        coverage=ResearchCoverage(target_met=True),
        boundaries=SearchBoundary(
            query_time="2026-08-04T00:00:00Z",
            max_candidates=10,
        ),
        evidence_index=evidence,
        query_trace=["关键陈述"],
    )


def _build(*, conflicted: bool = False) -> ResearchDossier:
    return StructuredDossierBuilder().build(
        _dossier(conflicted=conflicted),
        resolved=_resolved(),
        counterevidence_claim_refs=["claim_1"],
        evidence_set_revision=3,
        stop_reason="objective_satisfied",
    )


def _resolve_from_dossier(dossier: ResearchDossier, locator) -> str | None:
    for evidence in dossier.evidence_index.values():
        if evidence.locator == locator:
            return evidence.passage.text
    return None


def _context(clock: _Clock) -> ExecutionContext:
    return ExecutionContext(
        research_id="rsch_m3",
        attempt=1,
        policy_id="technical-landscape.v1",
        privacy=ResearchPrivacy(),
        deadline=Deadline.after(30_000, clock),
        cancellation=CancellationToken(),
        budget=BudgetLedger({}, monotonic=clock.monotonic),
    )


def _seed() -> SearchSeedSnapshot:
    return SearchSeedSnapshot(
        query=SearchQuery(
            original="关键陈述是否成立",
            effective="关键陈述是否成立",
            filters_requested=RequestedFilters(),
        ),
        evidence=[],
        retrieval_assessment=RetrievalAssessment(),
        retrieval_boundary=RetrievalBoundary(
            query_time=datetime(2026, 8, 4, tzinfo=timezone.utc),
            deadline_ms=30_000,
        ),
    )


def _task(clock: _Clock) -> ResearchTaskEnvelope:
    return ResearchTaskEnvelope(
        research_id="rsch_m3",
        state="running",
        phase="synthesizing",
        seed_search_id="srch_m3",
        seed_snapshot_hash="hash",
        created_at=clock.now(),
        updated_at=clock.now(),
        links=ResearchLinks(
            self="/research/rsch_m3",
            feedback="/research/rsch_m3/feedback",
            cancel="/research/rsch_m3/cancel",
        ),
    )


def test_structured_dossier_ids_are_stable_and_conflicts_are_preserved():
    first = _build(conflicted=True)
    second = _build(conflicted=True)

    assert first.findings[0].id == second.findings[0].id
    assert first.statements[0].id == second.statements[0].id
    assert first.summary is not None
    assert first.summary.key_finding_refs == [first.findings[0].id]
    assert first.conflicts[0].support_evidence_refs == ["evidence_support"]
    assert first.conflicts[0].conflict_evidence_refs == ["evidence_conflict"]
    assert first.statements[0].status == "conflicted"
    audit = CitationCoverageAuditor().audit(
        first,
        resolve_locator=lambda locator: _resolve_from_dossier(first, locator),
        audited_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    assert audit.status == "passed"


def test_citation_audit_fails_closed_for_unresolvable_fact():
    dossier = _build()
    audit = CitationCoverageAuditor().audit(
        dossier,
        resolve_locator=lambda locator: None,
        audited_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert audit.status == "failed"
    assert audit.citation_coverage_rate == 0
    assert audit.invalid_locator_refs == ["evidence_support"]


def test_synthesis_rejects_unknown_finding_and_restricted_mode_skips_gateway():
    clock = _Clock()
    dossier = _build()

    class Gateway:
        name = "external-spy"
        is_external = True

        def __init__(self) -> None:
            self.calls = 0

        def synthesize(self, request, **kwargs):
            self.calls += 1
            return SynthesisGatewayResult(
                draft=SynthesisDraft(statements=[ResearchStatement(
                    id="model",
                    text="模型自行生成的事实",
                    kind="factual",
                    status="supported",
                    finding_refs=["finding_unknown"],
                )]),
                model="model",
                input_tokens=11,
                output_tokens=7,
            )

    gateway = Gateway()
    synthesizer = ResearchSynthesizer(
        auditor=CitationCoverageAuditor(),
        gateway=gateway,
    )
    outcome = synthesizer.synthesize(
        dossier,
        question="关键陈述是否成立",
        profile="technology_landscape",
        allow_external_models=True,
        context=_context(clock),
        resolve_locator=lambda locator: _resolve_from_dossier(dossier, locator),
        now=clock.now(),
    )
    assert gateway.calls == 1
    assert outcome.mode == "model_fallback"
    assert outcome.failure_code == "SYNTHESIS_CITATION_AUDIT_FAILED"
    assert outcome.dossier.statements == dossier.statements

    restricted_gateway = Gateway()
    restricted = ResearchSynthesizer(
        auditor=CitationCoverageAuditor(),
        gateway=restricted_gateway,
    ).synthesize(
        dossier,
        question="关键陈述是否成立",
        profile="technology_landscape",
        allow_external_models=False,
        context=_context(clock),
        resolve_locator=lambda locator: _resolve_from_dossier(dossier, locator),
        now=clock.now(),
    )
    assert restricted_gateway.calls == 0
    assert restricted.mode == "deterministic"


def test_valid_model_synthesis_can_only_copy_supported_finding_text():
    clock = _Clock()
    dossier = _build()
    finding_id = dossier.findings[0].id

    class Gateway:
        name = "external-valid"
        is_external = True

        def synthesize(self, request, **kwargs):
            return SynthesisGatewayResult(
                draft=SynthesisDraft(statements=[ResearchStatement(
                    id="ignored",
                    text="关键陈述成立",
                    kind="factual",
                    status="supported",
                    finding_refs=[finding_id],
                )]),
                model="model",
                input_tokens=8,
                output_tokens=4,
            )

    outcome = ResearchSynthesizer(
        auditor=CitationCoverageAuditor(),
        gateway=Gateway(),
    ).synthesize(
        dossier,
        question="关键陈述是否成立",
        profile="technology_landscape",
        allow_external_models=True,
        context=_context(clock),
        resolve_locator=lambda locator: _resolve_from_dossier(dossier, locator),
        now=clock.now(),
    )

    assert outcome.mode == "model"
    assert outcome.failure_code is None
    assert outcome.model_requests == 1
    assert outcome.dossier.citation_audit is not None
    assert outcome.dossier.citation_audit.status == "passed"


def test_synthesis_cost_and_latency_are_bounded_to_one_deadline_aware_call():
    clock = _Clock()
    calls: list[float] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": '{"statements":[]}'}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3},
            }

    class Session:
        def post(self, *args, **kwargs):
            calls.append(kwargs["timeout"].total)
            return Response()

    result = SiliconFlowSynthesisGateway(
        api_key="token",
        base_url="https://example.test/v1",
        model="model",
        timeout=20,
        http_session=Session(),
    ).synthesize(
        SynthesisRequest(
            question="测试",
            profile="technology_landscape",
            headline="",
        ),
        deadline=Deadline.after(100, clock),
        cancellation=CancellationToken(),
    )

    assert calls == [0.1]
    assert result.input_tokens == 9
    assert result.output_tokens == 3


def test_external_synthesis_requires_explicit_enablement_and_key():
    assert Settings.from_env({}).research_synthesis_enabled is False
    with pytest.raises(ValueError, match="SILICONFLOW_API_KEY"):
        Settings.from_env({"RESEARCH_SYNTHESIS_ENABLED": "true"})

    configured = Settings.from_env({
        "RESEARCH_SYNTHESIS_ENABLED": "true",
        "SILICONFLOW_API_KEY": "token",
        "RESEARCH_SYNTHESIS_TIMEOUT": "7",
        "RESEARCH_ARTIFACT_RETENTION_SECONDS": "3600",
        "OPENALEX_ENABLED": "false",
    })
    assert configured.research_synthesis_enabled is True
    assert configured.research_synthesis_timeout == 7
    assert configured.research_artifact_retention_seconds == 3600


def test_synthesis_operation_and_artifacts_are_idempotent_and_retained(tmp_path):
    clock = _Clock()
    store = SqliteResearchStore(str(tmp_path / "m3.sqlite3"))
    task = _task(clock)
    store.create(
        task,
        idempotency_key="m3",
        request_hash="request",
        seed_snapshot=_seed(),
    )
    pending = ResearchSynthesisSnapshot(
        operation_id="synthesis_1",
        status="pending",
        created_at=clock.now(),
    )
    assert store.begin_synthesis(
        task.research_id, attempt=1, snapshot=pending
    ) is True
    assert store.begin_synthesis(
        task.research_id, attempt=1, snapshot=pending
    ) is False

    dossier = _build()
    dossier = dossier.model_copy(update={
        "citation_audit": CitationCoverageAuditor().audit(
            dossier,
            resolve_locator=lambda locator: _resolve_from_dossier(
                dossier, locator
            ),
            audited_at=clock.now(),
        )
    })
    service = ResearchArtifactService(
        store=store,
        clock=clock,
        retention_seconds=60,
    )
    first = service.create_all(
        task.research_id, dossier, evidence_set_revision=3
    )
    second = service.create_all(
        task.research_id, dossier, evidence_set_revision=3
    )

    assert len(first) == 4
    assert [item.artifact_id for item in first] == [
        item.artifact_id for item in second
    ]
    stored = service.get(task.research_id, first[0].artifact_id)
    assert hashlib.sha256(stored.content).hexdigest() == first[0].sha256
    assert ResearchArtifactRenderer._csv_safe("=cmd()") == "'=cmd()"
    clock.current += timedelta(seconds=61)
    try:
        service.get(task.research_id, first[0].artifact_id)
    except ResearchArtifactExpired:
        pass
    else:
        raise AssertionError("expired artifact should be rejected")
    store.close()


def test_pending_synthesis_recovers_without_repeating_external_operation(tmp_path):
    clock = _Clock()
    store = SqliteResearchStore(str(tmp_path / "recovery.sqlite3"))
    task = _task(clock)
    store.create(
        task,
        idempotency_key="m3-recovery",
        request_hash="request",
        seed_snapshot=_seed(),
    )
    builder = StructuredDossierBuilder()

    class Gateway:
        name = "external-spy"
        is_external = True
        calls = 0

        def synthesize(self, request, **kwargs):
            self.calls += 1
            raise AssertionError("recovery must not repeat the model call")

    gateway = Gateway()
    synthesizer = ResearchSynthesizer(
        auditor=CitationCoverageAuditor(),
        gateway=gateway,
    )
    operation_id = "synthesis_" + hashlib.sha256(
        "|".join((
            task.research_id,
            "1",
            "3",
            builder.version,
            synthesizer.version,
        )).encode("utf-8")
    ).hexdigest()[:20]
    store.begin_synthesis(
        task.research_id,
        attempt=1,
        snapshot=ResearchSynthesisSnapshot(
            operation_id=operation_id,
            status="pending",
            model_requests=1,
            created_at=clock.now(),
        ),
    )
    dossier = _build()
    store.resolve_locator = (  # type: ignore[method-assign]
        lambda research_id, locator: _resolve_from_dossier(dossier, locator)
    )
    runner = ResearchRunner.__new__(ResearchRunner)
    runner._task_store = store
    runner._clock = clock
    runner._dossier_builder = builder
    runner._synthesizer = synthesizer

    restored, failure = runner._synthesize(
        task.research_id,
        attempt=1,
        dossier=dossier,
        resolved=_resolved(),
        model_route=ResolvedModelRoute(
            rewrite="configured",
            rerank="configured",
            verify="configured",
            synthesis="configured",
            allow_external_models=True,
        ),
        context=_context(clock),
        evidence_set_revision=3,
    )

    assert gateway.calls == 0
    assert failure is not None
    assert failure.code == "SYNTHESIS_RECOVERY_FALLBACK"
    assert restored.citation_audit is not None
    assert restored.citation_audit.status == "passed"
    ready = store.get_synthesis(
        task.research_id, operation_id=operation_id
    )
    assert ready is not None and ready.status == "ready"
    assert ready.model_requests == 1
    store.close()


def test_artifact_download_requires_auth_and_matches_manifest(tmp_path):
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
        mcp_mode="false",
        state_db_path=str(tmp_path / "api.sqlite3"),
        research_max_workers=1,
        api_auth_token="secret-token",
    )
    container = build_container(settings, include_mcp=False)
    seed = container.seed_store.save(_seed(), ttl_seconds=3600)
    headers = {
        "Authorization": "Bearer secret-token",
        "Idempotency-Key": "m3-artifact-api",
    }
    with TestClient(create_app(container)) as client:
        started = client.post(
            "/research",
            headers=headers,
            json={
                "search_id": seed.search_id,
                "profile": "technology_landscape",
                "depth": "quick",
            },
        )
        assert started.status_code == 202
        research_id = started.json()["research_id"]
        task = {}
        for _ in range(200):
            response = client.get(
                f"/research/{research_id}?detail=full",
                headers={"Authorization": "Bearer secret-token"},
            )
            task = response.json()
            if task["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        artifacts = task["dossier"]["artifact_index"]
        assert task["dossier"]["citation_audit"]["status"] == "passed"
        assert len(artifacts) == 4
        target = artifacts[0]
        assert client.get(target["href"]).status_code == 401
        downloaded = client.get(
            target["href"],
            headers={"Authorization": "Bearer secret-token"},
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["etag"] == f'"{target["sha256"]}"'
        assert hashlib.sha256(downloaded.content).hexdigest() == target["sha256"]
