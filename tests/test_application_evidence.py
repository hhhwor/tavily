"""Unit tests for the application EvidenceAssembler."""
from src.application.evidence_assembler import EvidenceAssembler
from src.models import AcademicResult, PatentResult, SearchResult


def test_assemble_maps_three_domains_and_sorts_by_relevance():
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
        citations=12,
        is_oa=True,
        oa_pdf_url="https://arxiv.org/pdf/1234.5678",
        license="cc-by",
        pdf_status="ready",
        pdf_text="pdf evidence text",
        pdf_chunk_index=2,
        pdf_page_from=4,
        pdf_page_to=5,
        pdf_next_cursor="cursor1",
        rerank_score=0.9,
        raw={"language": "en"},
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
    inputs_before = [
        web.model_dump(),
        paper.model_dump(),
        patent.model_dump(),
    ]

    evidence = EvidenceAssembler().assemble([web], [paper], [patent])

    assert [item.type for item in evidence] == ["academic", "patent", "web"]
    assert [web.model_dump(), paper.model_dump(), patent.model_dump()] == inputs_before

    academic = evidence[0]
    assert academic.id == "academic:W123:pdf:2"
    assert academic.result_id == "academic:W123"
    assert academic.url == "https://doi.org/10.1000/example"
    assert academic.language == "en"
    assert academic.passage.model_dump() == {
        "text": "pdf evidence text",
        "snippet_type": "pdf_text",
        "char_start": 0,
        "char_end": len("pdf evidence text"),
        "page_from": 4,
        "page_to": 5,
        "chunk_index": 2,
    }
    assert academic.citation.label == "Ada Lovelace et al., 2025"
    assert academic.citation.authors == ["Ada Lovelace", "Alan Turing"]
    assert academic.citation.doi == "10.1000/example"
    assert academic.scores.authority == 12.0
    assert academic.access.license == "cc-by"
    assert academic.access.next_cursor == "cursor1"
    assert academic.diagnostics.partial is True
    assert academic.diagnostics.warnings == ["TRUNCATED_EVIDENCE"]

    patent_evidence = evidence[1]
    assert patent_evidence.id == "patent:US-1-A1:abstract"
    assert patent_evidence.citation.publication_number == "US-1-A1"
    assert patent_evidence.patent is not None
    assert patent_evidence.patent.model_dump() == {
        "publication_number": "US-1-A1",
        "application_number": "US-APP-1",
        "applicant": ["Acme Corp"],
        "inventor": ["Jane Inventor"],
        "ipc_main": "H01M",
        "cpc_main": "H01M10/00",
        "country": "US",
        "status": "active",
        "family_id": "F1",
        "application_date": "2023-01-01",
        "publication_date": "2024-01-01",
        "patent_type": "A1",
        "citation_count": 5,
    }

    web_evidence = evidence[2]
    assert web_evidence.passage.snippet_type == "web_content"
    assert web_evidence.citation.label == "example.com"
    assert web_evidence.scores.relevance == 0.6


def test_assemble_preserves_abstract_pdf_warnings_and_url_fallback():
    paper = AcademicResult(
        url="",
        title="Paper",
        content="abstract only evidence",
        source="openalex_local",
        work_id="W1",
        year=2024,
        oa_landing_url="https://openalex.org/W1",
        oa_pdf_url="https://example.com/paper.pdf",
        pdf_status="no_pdf_url",
        pdf_error_code="PDF_URL_MISSING",
        rerank_score=0.8,
    )

    evidence = EvidenceAssembler().assemble([], [paper], [])

    assert len(evidence) == 1
    item = evidence[0]
    assert item.url == "https://openalex.org/W1"
    assert item.published_date == "2024"
    assert item.id == "academic:W1:abstract"
    assert item.passage.snippet_type == "abstract"
    assert item.passage.chunk_index is None
    assert item.diagnostics.warnings == [
        "PDF_TEXT_UNAVAILABLE",
        "PDF_URL_MISSING",
    ]
    assert item.diagnostics.failure_code == "PDF_URL_MISSING"


def test_assemble_clips_at_legacy_limit_and_marks_partial():
    result = SearchResult(
        url="https://example.com/long",
        title="Long",
        content="x" * 1801,
        source="web",
    )

    item = EvidenceAssembler().assemble([result], [], [])[0]

    assert item.passage.text == "x" * 1800 + "…"
    assert item.passage.char_end == 1801
    assert item.diagnostics.partial is True
    assert item.diagnostics.warnings == ["TRUNCATED_EVIDENCE"]


def test_assemble_skips_empty_text_but_keeps_original_rank_fallback():
    empty = SearchResult(url="", source="web")
    snippet = SearchResult(
        url="https://example.com/snippet",
        title="",
        snippet="search snippet",
        source="web",
    )

    evidence = EvidenceAssembler().assemble([empty, snippet], [], [])

    assert len(evidence) == 1
    assert evidence[0].passage.snippet_type == "web_snippet"
    assert evidence[0].scores.source_rank == 1
    assert evidence[0].scores.relevance == 0.5
    assert evidence[0].scores.confidence == 0.5
