from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api import create_app
from src.application.academic_document_reader import AcademicDocumentReader
from src.application.evidence_adoption import EvidenceAdoptionGate
from src.application.outcomes import PdfEnrichmentOutcome
from src.application.ports.runtime import Deadline
from src.application.research_coverage import CoverageEvaluator
from src.application.research_execution import (
    BudgetLedger,
    CancellationToken,
    ExecutionContext,
)
from src.application.research_planner import ResearchPlanner
from src.bootstrap import build_container
from src.config import Settings
from src.domain.documents import EnrichedDocument
from src.domain.document_read import (
    DocumentReadDiagnostics,
    DocumentReadResult,
)
from src.domain.evidence import (
    Evidence,
    EvidenceAccess,
    EvidenceCitation,
    EvidenceDiagnostics,
    EvidenceLocator,
    EvidencePassage,
    EvidenceProvenance,
    EvidenceQuality,
)
from src.domain.pdf_text import PdfTextPage
from src.domain.research import (
    ObjectivePlan,
    ResearchLinks,
    ResearchPrivacy,
    ResearchTaskEnvelope,
)
from src.domain.search import AcademicResult
from src.domain.search_api import (
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchQuery,
    SearchSeedSnapshot,
)
from src.domain.trust import CandidateClaim, ClaimAssessment
from src.infrastructure.sqlite_research_store import SqliteResearchStore


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> datetime:
        return datetime(2026, 8, 4, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value


class _PdfGateway:
    def __init__(self) -> None:
        self.enrich_calls = 0
        self.page_calls: list[str | None] = []

    def enrich(self, papers, **kwargs):
        self.enrich_calls += 1
        ranked = papers[0]
        paper = ranked.to_result()
        assert isinstance(paper, AcademicResult)
        paper = paper.model_copy(update={
            "pdf_status": "ready",
            "pdf_text": "研究背景与实验设计。",
            "pdf_text_length": 48,
            "pdf_returned_chars": 10,
            "pdf_chunk_index": 0,
            "pdf_page_from": 1,
            "pdf_page_to": 1,
            "pdf_next_cursor": "cursor-1",
            "pdf_content_hash": "sha256:upstream-version-1",
            "pdf_parser_version": "grobid-1.2",
            "pdf_version_id": "upstream-version-1",
        })
        return PdfEnrichmentOutcome(
            academic=(EnrichedDocument.from_result(ranked, paper),),
        )

    def read_page(
        self,
        work_id,
        cursor=None,
        max_chars=None,
        *,
        deadline=None,
    ):
        self.page_calls.append(cursor)
        return PdfTextPage(
            work_id=work_id,
            status="ready",
            chunk_index=1,
            page_from=2,
            page_to=2,
            text="实验结果表明，固态电池界面阻抗降低。",
            returned_chars=19,
            content_hash="sha256:upstream-version-1",
            parser_version="grobid-1.2",
        )


def _candidate() -> Evidence:
    return Evidence(
        id="academic:W123:abstract",
        result_id="academic:W123",
        type="academic",
        source="openalex",
        title="固态电池界面研究",
        url="https://doi.org/10.1000/example",
        published_date="2025-01-01",
        passage=EvidencePassage(
            text="摘要认为固态电池界面阻抗降低。",
            snippet_type="abstract",
            char_start=0,
            char_end=16,
        ),
        citation=EvidenceCitation(
            label="Example et al., 2025",
            doi="10.1000/example",
            work_id="W123",
        ),
        access=EvidenceAccess(
            is_open=True,
            license="cc-by",
            oa_pdf_url="https://example.test/paper.pdf",
        ),
        diagnostics=EvidenceDiagnostics(warnings=["ABSTRACT_ONLY"]),
        provenance=EvidenceProvenance(
            canonical_url="https://doi.org/10.1000/example",
            retrieved_via="openalex",
            content_origin="metadata",
            document_id="W123",
            version_id="10.1000/example",
            retrieved_at="2026-08-04T00:00:00Z",
            license="cc-by",
        ),
        locator=EvidenceLocator(
            document_id="W123",
            version_id="10.1000/example",
            section="abstract",
        ),
        quality=EvidenceQuality(
            level="discovery_only",
            has_stable_locator=True,
            reasons=["ABSTRACT_ONLY"],
        ),
    )


def _context(clock: _Clock) -> ExecutionContext:
    return ExecutionContext(
        research_id="rsch_m2",
        attempt=1,
        policy_id="technical-landscape.v1",
        privacy=ResearchPrivacy(),
        deadline=Deadline.after(30_000, clock),
        cancellation=CancellationToken(),
        budget=BudgetLedger({}, monotonic=clock.monotonic),
    )


def _snapshot(evidence: list[Evidence]) -> SearchSeedSnapshot:
    return SearchSeedSnapshot(
        requested_source_types=["academic"],
        planned_source_types=["academic"],
        query=SearchQuery(
            original="固态电池界面阻抗是否降低",
            effective="固态电池界面阻抗是否降低",
            filters_requested=RequestedFilters(),
        ),
        evidence=evidence,
        retrieval_assessment=RetrievalAssessment(status="limited"),
        retrieval_boundary=RetrievalBoundary(
            query_time=datetime(2026, 8, 4, tzinfo=timezone.utc),
            deadline_ms=30_000,
        ),
    )


def test_academic_reader_continues_cursor_and_adopts_resolvable_version():
    clock = _Clock()
    gateway = _PdfGateway()
    result = AcademicDocumentReader(
        gateway,
        now=clock.now,
    ).read(_candidate(), context=_context(clock))

    assert result.status == "ready"
    assert result.version is not None
    assert result.version.stable is True
    assert result.version.content_hash == "sha256:upstream-version-1"
    assert result.version.parser_version == "grobid-1.2"
    assert [chunk.locator.page_from for chunk in result.chunks] == [1, 2]
    assert gateway.page_calls == ["cursor-1"]

    adopted = EvidenceAdoptionGate().adopt(
        _candidate(),
        result,
        claim_texts=["固态电池界面阻抗降低"],
    )
    support = next(
        item for item in adopted if "界面阻抗降低" in item.passage.text
    )
    assert support.quality is not None
    assert support.quality.can_support_key_claim is True
    assert support.locator is not None
    source_chunk = next(
        item for item in result.chunks
        if item.chunk_index == support.locator.chunk_index
    )
    assert support.passage.text == source_chunk.text[
        support.locator.char_start:support.locator.char_end
    ]


def test_planner_routes_abstract_gap_to_deep_read():
    candidate = _candidate()
    claim = CandidateClaim(
        id="claim_1",
        text="固态电池界面阻抗降低",
        importance="key",
    )
    plan = ObjectivePlan(question=claim.text, claims=[claim])
    assessment = ClaimAssessment(
        claim=claim,
        support_refs=[candidate.id],
        gaps=["NO_CITABLE_SUPPORT", "ABSTRACT_ONLY"],
    )
    coverage = CoverageEvaluator().evaluate(plan, [candidate], [assessment])

    action = ResearchPlanner().next_actions(
        plan,
        coverage,
        round_number=1,
        evidence=[candidate],
    )[0]

    assert action.kind == "deep_read"
    assert action.candidate_ids == [candidate.id]
    assert action.query is None
    assert action.target_gap_refs


def test_incomplete_version_is_not_qualified_and_failure_becomes_gap():
    clock = _Clock()
    candidate = _candidate()
    complete = AcademicDocumentReader(
        _PdfGateway(),
        now=clock.now,
    ).read(candidate, context=_context(clock))
    assert complete.version is not None
    unstable = complete.model_copy(update={
        "status": "partial",
        "version": complete.version.model_copy(update={
            "complete": False,
            "content_hash_scope": "observed_chunks",
        }),
        "diagnostics": DocumentReadDiagnostics(
            warnings=["PDF_TEXT_TIMEOUT", "DOCUMENT_VERSION_INCOMPLETE"],
            failure_code="PDF_TEXT_TIMEOUT",
            message="Academic PDF read is partial.",
        ),
    })
    adopted = EvidenceAdoptionGate().adopt(
        candidate,
        unstable,
        claim_texts=["固态电池界面阻抗降低"],
    )
    assert adopted
    assert all(
        item.quality is not None
        and item.quality.can_support_key_claim is False
        for item in adopted
    )

    marked = EvidenceAdoptionGate().mark_failure(
        candidate,
        DocumentReadResult(
            status="failed",
            diagnostics=DocumentReadDiagnostics(
                warnings=["PDF_TEXT_TIMEOUT"],
                failure_code="PDF_TEXT_TIMEOUT",
                message="Academic PDF text is unavailable.",
            ),
        ),
    )
    claim = CandidateClaim(id="claim_1", text="固态电池界面阻抗降低")
    coverage = CoverageEvaluator().evaluate(
        ObjectivePlan(question=claim.text, claims=[claim]),
        [marked],
        [ClaimAssessment(
            claim=claim,
            gaps=["NO_SUPPORTING_EVIDENCE"],
        )],
    )
    assert any(gap.code == "PDF_TEXT_TIMEOUT" for gap in coverage.gaps)


def test_document_chunks_are_stored_outside_task_and_locator_resolves(tmp_path):
    clock = _Clock()
    candidate = _candidate()
    result = AcademicDocumentReader(
        _PdfGateway(),
        now=clock.now,
    ).read(candidate, context=_context(clock))
    adopted = EvidenceAdoptionGate().adopt(
        candidate,
        result,
        claim_texts=["固态电池界面阻抗降低"],
    )
    store = SqliteResearchStore(str(tmp_path / "research.sqlite3"))
    task = ResearchTaskEnvelope(
        research_id="rsch_m2",
        state="running",
        phase="deep_reading",
        seed_search_id="srch_m2",
        seed_snapshot_hash="seed-hash",
        created_at=clock.now(),
        updated_at=clock.now(),
        links=ResearchLinks(
            self="/research/rsch_m2",
            feedback="/research/rsch_m2/feedback",
            cancel="/research/rsch_m2/cancel",
        ),
    )
    store.create(
        task,
        idempotency_key="m2-store",
        request_hash="request-hash",
        seed_snapshot=_snapshot([candidate]),
    )
    store.save_document_read(
        task.research_id,
        attempt=1,
        action_id="action-deep-read",
        result=result,
    )

    restored = store.get_document_read(
        task.research_id,
        action_id="action-deep-read",
    )
    assert restored == result
    support = next(
        item for item in adopted if "界面阻抗降低" in item.passage.text
    )
    assert support.locator is not None
    assert store.resolve_locator(task.research_id, support.locator) == (
        support.passage.text
    )
    task_payload = store._connection.execute(  # noqa: SLF001
        "SELECT payload FROM research_tasks WHERE research_id = ?",
        (task.research_id,),
    ).fetchone()["payload"]
    assert "实验结果表明" not in task_payload
    store.close()


def test_research_runner_executes_gap_driven_academic_deep_read(tmp_path):
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
        "academic"
    ] = AcademicDocumentReader(_PdfGateway())
    seed = container.seed_store.save(
        _snapshot([_candidate()]),
        ttl_seconds=3600,
    )
    with TestClient(create_app(container)) as client:
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m2-academic-runner"},
            json={
                "search_id": seed.search_id,
                "profile": "technology_landscape",
                "depth": "quick",
                "objective": {
                    "question": "固态电池界面阻抗降低",
                    "claims": [{"text": "固态电池界面阻抗降低"}],
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
        pdf_evidence = next(
            item for item in task["dossier"]["evidence_index"].values()
            if item["passage"]["snippet_type"] == "pdf_text"
            and "界面阻抗降低" in item["passage"]["text"]
        )
        locator = EvidenceLocator.model_validate(pdf_evidence["locator"])
        assert container.research_store.resolve_locator(
            research_id,
            locator,
        ) == pdf_evidence["passage"]["text"]
