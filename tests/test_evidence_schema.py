"""Evidence schema construction tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.application.answerability import AnswerabilityPolicy
from src.application.evidence_assembler import EvidenceAssembler
from src.l0 import plan_query
from src.models import AcademicResult, PatentResult, SearchFailure, SearchResponse, SearchResult


def test_build_evidence_mixes_sources_by_relevance():
    web = SearchResult(
        url="https://example.com/web",
        title="Web Result",
        content="web evidence text",
        source="web_provider",
        date="2026-01-01",
        site="example.com",
        rerank_score=0.6,
    )
    paper = AcademicResult(
        url="https://doi.org/10.1000/example",
        title="Academic Result",
        content="abstract text",
        source="openalex_local",
        authors=["Ada Lovelace", "Alan Turing"],
        work_id="W123",
        year=2025,
        venue="arXiv",
        doi="10.1000/example",
        is_oa=True,
        oa_pdf_url="https://arxiv.org/pdf/1234.5678",
        license="cc-by",
        pdf_status="ready",
        pdf_text="pdf evidence text",
        pdf_next_cursor="cursor1",
        rerank_score=0.9,
    )
    patent = PatentResult(
        url="https://patents.example/P1",
        title="Patent Result",
        content="patent evidence text",
        source="patent_es",
        publication_number="US-1-A1",
        application_number="US-APP-1",
        applicant=["Acme Corp"],
        inventor=["Jane Inventor"],
        ipc_main="H01M",
        cpc_main="H01M10/00",
        application_date="2023-01-01",
        publication_date="2024-01-01",
        patent_type="A1",
        country="US",
        status="active",
        family_id="F1",
        citation_count=5,
        rerank_score=0.7,
    )

    evidence = EvidenceAssembler().assemble([web], [paper], [patent])

    assert [e.type for e in evidence] == ["academic", "patent", "web"]
    top = evidence[0]
    assert top.id == "academic:W123:pdf:0"
    assert top.result_id == "academic:W123"
    assert top.passage.snippet_type == "pdf_text"
    assert top.passage.text == "pdf evidence text"
    assert top.citation.label == "Ada Lovelace et al., 2025"
    assert top.access.license == "cc-by"
    assert top.access.next_cursor == "cursor1"
    assert top.diagnostics.partial is True
    assert "TRUNCATED_EVIDENCE" in top.diagnostics.warnings

    patent_evidence = evidence[1]
    assert patent_evidence.citation.publication_number == "US-1-A1"
    assert patent_evidence.patent is not None
    assert patent_evidence.patent.publication_number == "US-1-A1"
    assert patent_evidence.patent.application_number == "US-APP-1"
    assert patent_evidence.patent.applicant == ["Acme Corp"]
    assert patent_evidence.patent.inventor == ["Jane Inventor"]
    assert patent_evidence.patent.ipc_main == "H01M"
    assert patent_evidence.patent.cpc_main == "H01M10/00"
    assert patent_evidence.patent.country == "US"
    assert patent_evidence.patent.status == "active"
    assert patent_evidence.patent.family_id == "F1"
    assert patent_evidence.patent.application_date == "2023-01-01"
    assert patent_evidence.patent.publication_date == "2024-01-01"
    assert patent_evidence.patent.patent_type == "A1"
    assert patent_evidence.patent.citation_count == 5


def test_build_evidence_marks_pdf_gap_for_abstract_only_paper():
    paper = AcademicResult(
        url="https://openalex.org/W1",
        title="Paper",
        content="abstract only evidence",
        source="openalex_local",
        work_id="W1",
        oa_pdf_url="https://example.com/paper.pdf",
        pdf_status="not_requested",
        rerank_score=0.8,
    )

    evidence = EvidenceAssembler().assemble([], [paper], [])

    assert len(evidence) == 1
    assert evidence[0].passage.snippet_type == "abstract"
    assert "PDF_TEXT_UNAVAILABLE" in evidence[0].diagnostics.warnings


def test_search_response_exposes_only_evidence_results():
    resp = SearchResponse(
        query="q",
        evidence=[],
        count=0,
        providers_used=[],
        reranker="none",
        elapsed_ms=1,
    )

    data = resp.model_dump()

    assert "evidence" in data
    assert "answerability" in data
    assert "failures" in data
    assert "partial_failure" in data
    assert data["answerability"]["status"] == "not_answerable"
    assert "results" not in data
    assert "academic_results" not in data
    assert "patent_results" not in data


def test_answerability_reports_gaps_and_partial_failure():
    failure = SearchFailure(
        stage="provider_search",
        source="openalex_local",
        type="academic",
        code="PROVIDER_SEARCH_FAILED",
        message="boom",
    )

    answerability = AnswerabilityPolicy().evaluate(
        [],
        [failure],
        expected_web=False,
        expected_academic=True,
        expected_patent=False,
        include_pdf_text=False,
    )

    codes = [gap.code for gap in answerability.gaps]
    assert answerability.status == "not_answerable"
    assert answerability.confidence == "none"
    assert "NO_EVIDENCE" in codes
    assert "PARTIAL_FAILURE" in codes
    assert "NO_ACADEMIC_EVIDENCE" in codes


def test_plan_query_records_rewrite_failure():
    class FailedSession:
        def post(self, *args, **kwargs):
            raise RuntimeError("rewrite api down")

    plan = plan_query(
        "what is rag",
        ["baidu"],
        rewrite=True,
        rewrite_api_key="token",
        http_session=FailedSession(),
    )

    assert plan.rewritten_query == "what is rag"
    assert len(plan.failures) == 1
    assert plan.failures[0].code == "QUERY_REWRITE_FAILED"
