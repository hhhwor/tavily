from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api import create_app
from src.application.evidence_adoption import EvidenceAdoptionGate
from src.application.patent_document_reader import PatentDocumentReader
from src.application.research_coverage import CoverageEvaluator
from src.application.research_planner import ResearchPlanner
from src.application.ports.runtime import Deadline
from src.application.research_execution import (
    BudgetLedger,
    CancellationToken,
    ExecutionContext,
)
from src.bootstrap import build_container
from src.config import Settings
from src.domain.evidence import (
    Evidence,
    EvidenceAccess,
    EvidenceCitation,
    EvidenceDiagnostics,
    EvidenceLocator,
    EvidencePassage,
    EvidencePatent,
    EvidenceProvenance,
    EvidenceQuality,
)
from src.domain.patent_text import PatentDocumentRecord, PatentTextUnit
from src.domain.research import ObjectivePlan, ResearchPrivacy
from src.domain.search_api import (
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchQuery,
    SearchSeedSnapshot,
)
from src.infrastructure.patent_es_fulltext import PatentEsFullTextGateway
from src.domain.trust import CandidateClaim, ClaimAssessment


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 4, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return 0.0


class _PatentGateway:
    def fetch(self, publication_number, *, deadline):
        return PatentDocumentRecord(
            status="ready",
            publication_number=publication_number,
            application_number="CN202410000001",
            family_id="family-1",
            priority_root="CN202310000001",
            priority_dates=["2023-01-02"],
            application_date="2024-01-02",
            publication_date="2025-01-02",
            canonical_uri=f"https://patents.example/{publication_number}",
            units=[
                PatentTextUnit(
                    kind="claim",
                    identifier="1",
                    text="一种固态电池，其界面涂层使界面阻抗降低。",
                ),
                PatentTextUnit(
                    kind="description",
                    identifier="0042",
                    section="具体实施方式",
                    text="实施例采用复合界面涂层。",
                ),
            ],
            family_members=["CN123456B", "WO2025000001A1"],
            patent_citations=["CN111111A"],
            npl_citations=["doi:10.1000/prior"],
            license="patent-public-record",
        )


def _context(research_id: str = "rsch_patent") -> ExecutionContext:
    clock = _Clock()
    return ExecutionContext(
        research_id=research_id,
        attempt=1,
        policy_id="technical-landscape.v1",
        privacy=ResearchPrivacy(),
        deadline=Deadline.after(30_000, clock),
        cancellation=CancellationToken(),
        budget=BudgetLedger({}, monotonic=clock.monotonic),
    )


def _candidate() -> Evidence:
    return Evidence(
        id="patent:CN123456A:abstract",
        result_id="patent:CN123456A",
        type="patent",
        source="patent_es",
        title="固态电池界面涂层",
        url="https://patents.example/CN123456A",
        published_date="2025-01-02",
        passage=EvidencePassage(
            text="摘要称界面涂层使界面阻抗降低。",
            snippet_type="patent_abstract",
        ),
        citation=EvidenceCitation(
            label="CN123456A",
            publication_number="CN123456A",
        ),
        patent=EvidencePatent(
            publication_number="CN123456A",
            family_id="family-1",
            country="CN",
            application_date="2024-01-02",
            publication_date="2025-01-02",
        ),
        access=EvidenceAccess(is_open=True),
        diagnostics=EvidenceDiagnostics(
            warnings=["PATENT_ABSTRACT_ONLY", "CLAIM_TEXT_UNAVAILABLE"]
        ),
        provenance=EvidenceProvenance(
            canonical_url="https://patents.example/CN123456A",
            publisher_type="patent_authority",
            content_origin="metadata",
            document_id="CN123456A",
            version_id="CN123456A",
            retrieved_at="2026-08-04T00:00:00Z",
        ),
        locator=EvidenceLocator(
            document_id="CN123456A",
            version_id="CN123456A",
            section="abstract",
        ),
        quality=EvidenceQuality(
            level="discovery_only",
            has_stable_locator=True,
            reasons=["PATENT_ABSTRACT_ONLY", "CLAIM_TEXT_UNAVAILABLE"],
        ),
    )


def _snapshot() -> SearchSeedSnapshot:
    return SearchSeedSnapshot(
        requested_source_types=["patent"],
        planned_source_types=["patent"],
        query=SearchQuery(
            original="界面涂层使界面阻抗降低",
            effective="界面涂层使界面阻抗降低",
            filters_requested=RequestedFilters(),
        ),
        evidence=[_candidate()],
        retrieval_assessment=RetrievalAssessment(status="limited"),
        retrieval_boundary=RetrievalBoundary(
            query_time=datetime(2026, 8, 4, tzinfo=timezone.utc),
            deadline_ms=30_000,
        ),
    )


def test_patent_reader_preserves_claim_specification_family_and_citations():
    candidate = _candidate()
    result = PatentDocumentReader(
        _PatentGateway(),
        now=_Clock().now,
    ).read(candidate, context=_context())

    assert result.status == "ready"
    assert result.version is not None
    assert result.version.independent_work_id == "patent-family:family-1"
    assert result.version.relations["priority_root"] == ["CN202310000001"]
    assert result.version.relations["patent_citations"] == ["CN111111A"]
    assert result.chunks[0].locator.claim_number == "1"
    assert result.chunks[1].locator.paragraph_id == "0042"

    adopted = EvidenceAdoptionGate(max_passages=10).adopt(
        candidate,
        result,
        claim_texts=["界面涂层使界面阻抗降低"],
    )
    claim = next(item for item in adopted if item.locator.claim_number)
    specification = next(
        item for item in adopted if item.locator.paragraph_id
    )
    assert claim.passage.snippet_type == "patent_claim"
    assert claim.quality.can_support_key_claim is True
    assert claim.patent.family_members == [
        "CN123456B", "WO2025000001A1"
    ]
    assert claim.patent.npl_citations == ["doi:10.1000/prior"]
    assert specification.passage.snippet_type == "patent_specification"
    assert specification.quality.can_support_key_claim is False
    assert "PATENT_SPECIFICATION_CONTEXT_ONLY" in specification.quality.reasons


def test_patent_fulltext_es_adapter_normalizes_optional_fulltext_fields():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"hits": {"hits": [{
                "_index": "patent-fulltext-v1",
                "_id": "CN123456A",
                "_version": 3,
                "_source": {
                    "publication_number": "CN123456A",
                    "application_number": "CN202410000001",
                    "family_id": "family-1",
                    "priority_root": "CN202310000001",
                    "claims": [{
                        "claim_number": "1",
                        "text": "一种固态电池。",
                    }],
                    "description_paragraphs": [{
                        "paragraph_id": "0042",
                        "text": "实施例说明。",
                    }],
                    "family_members": ["CN123456B"],
                    "patent_citations": ["CN111111A"],
                    "npl_citations": [{"id": "doi:10.1000/prior"}],
                },
            }]}}

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    gateway = PatentEsFullTextGateway(
        base_url="https://patent-fulltext.test",
        index="patent-fulltext-read",
        http_session=session,
    )
    record = gateway.fetch(
        "CN123456A",
        deadline=_context().deadline,
    )

    assert record.status == "ready"
    assert [unit.identifier for unit in record.units] == ["1", "0042"]
    assert record.npl_citations == ["doi:10.1000/prior"]
    assert record.source_version_id == (
        "patent-fulltext-v1:CN123456A:3"
    )
    assert session.calls[0][0].endswith(
        "/patent-fulltext-read/_search"
    )


def test_prior_art_coverage_routes_family_before_citation_expansion():
    candidate = _candidate()
    result = PatentDocumentReader(_PatentGateway()).read(
        candidate,
        context=_context(),
    )
    claim_evidence = next(
        item for item in EvidenceAdoptionGate(max_passages=10).adopt(
            candidate,
            result,
            claim_texts=["界面涂层使界面阻抗降低"],
        )
        if item.locator is not None and item.locator.claim_number
    )
    claim = CandidateClaim(
        id="claim_1",
        text="界面涂层使界面阻抗降低",
    )
    plan = ObjectivePlan(
        question=claim.text,
        profile="prior_art_landscape",
        claims=[claim],
    )
    coverage = CoverageEvaluator().evaluate(
        plan,
        [claim_evidence],
        [ClaimAssessment(
            claim=claim,
            status="supported",
            support_refs=[claim_evidence.id],
            counterevidence_searched=True,
        )],
    )

    action = ResearchPlanner().next_actions(
        plan,
        coverage,
        round_number=2,
        evidence=[claim_evidence],
    )[0]

    assert action.kind == "family_expand"
    assert action.candidate_ids == [claim_evidence.id]
    assert action.related_document_ids == ["CN123456B"]


def test_research_runner_executes_patent_claim_deep_read(tmp_path):
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
    container.engine._research_service._runner._document_readers[  # noqa: SLF001
        "patent"
    ] = PatentDocumentReader(_PatentGateway())
    seed = container.seed_store.save(_snapshot(), ttl_seconds=3600)
    with TestClient(create_app(container)) as client:
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m2-patent-runner"},
            json={
                "search_id": seed.search_id,
                "profile": "technology_landscape",
                "depth": "quick",
                "objective": {
                    "question": "界面涂层使界面阻抗降低",
                    "claims": [{"text": "界面涂层使界面阻抗降低"}],
                },
            },
        )
        assert started.status_code == 202
        research_id = started.json()["research_id"]
        task = {}
        for _ in range(200):
            task = client.get(
                f"/research/{research_id}?detail=full"
            ).json()
            if task["state"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.01)

        assert task["state"] == "completed"
        assert task["dossier"]["rounds"][0]["actions"][0]["kind"] == (
            "deep_read"
        )
        claim = next(
            item for item in task["dossier"]["evidence_index"].values()
            if item["passage"]["snippet_type"] == "patent_claim"
        )
        locator = EvidenceLocator.model_validate(claim["locator"])
        assert container.research_store.resolve_locator(
            research_id,
            locator,
        ) == claim["passage"]["text"]


def test_prior_art_runner_checkpoints_family_and_citation_expansion(tmp_path):
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
    container.engine._research_service._runner._document_readers[  # noqa: SLF001
        "patent"
    ] = PatentDocumentReader(_PatentGateway())
    seed = container.seed_store.save(_snapshot(), ttl_seconds=3600)
    with TestClient(create_app(container)) as client:
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m2-prior-art-expansion"},
            json={
                "search_id": seed.search_id,
                "profile": "prior_art_landscape",
                "depth": "deep",
                "objective": {
                    "question": "界面涂层使界面阻抗降低",
                    "claims": [{"text": "界面涂层使界面阻抗降低"}],
                },
            },
        )
        research_id = started.json()["research_id"]
        waiting = {}
        for _ in range(200):
            waiting = client.get(f"/research/{research_id}").json()
            if waiting["state"] == "needs_input":
                break
            time.sleep(0.01)
        feedback = client.post(
            f"/research/{research_id}/feedback",
            json={
                "task_revision": waiting["task_revision"],
                "answers": {"jurisdictions": "CN, WO"},
            },
        )
        assert feedback.status_code == 200
        task = {}
        for _ in range(400):
            task = client.get(
                f"/research/{research_id}?detail=full"
            ).json()
            if task["state"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.01)

        kinds = [
            round_result["actions"][0]["kind"]
            for round_result in task["dossier"]["rounds"]
        ]
        assert kinds[:4] == [
            "deep_read",
            "family_expand",
            "family_expand",
            "citation_expand",
        ]
        related = [
            round_result["actions"][0]["related_document_ids"]
            for round_result in task["dossier"]["rounds"][1:4]
        ]
        assert related == [
            ["CN123456B"],
            ["WO2025000001A1"],
            ["CN111111A"],
        ]
