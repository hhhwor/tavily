"""Phase 0 provenance、locator、quality 与 SearchBoundary 测试。"""
import json
from datetime import datetime, timezone

import pytest

from src.application.evidence_assembler import EvidenceAssembler
from src.bootstrap import build_container
from src.config import Settings
from src.models import AcademicResult, PatentResult, SearchResult
from src.trust import annotate_evidence, build_search_boundary


@pytest.fixture
def empty_engine():
    container = build_container(
        Settings(
            openalex_enabled=False,
            patent_es_enabled=False,
            ranking_profile="fast",
            rerank_threshold_mode="off",
            mcp_mode="false",
            state_db_path=":memory:",
        ),
        include_mcp=False,
    )
    try:
        yield container.engine
    finally:
        container.close()


def test_phase0_annotates_web_academic_and_patent_without_reordering():
    web = SearchResult(
        url="https://www.example.com/news/?utm_source=test",
        title="Web",
        content="provider extracted content",
        source="tencent",
        site="Example",
        date="2026-07-01",
        rerank_score=0.9,
    )
    paper = AcademicResult(
        url="https://doi.org/10.1000/example",
        title="Paper",
        content="paper abstract",
        source="openalex_local",
        work_id="W1",
        doi="10.1000/example",
        venue="Journal",
        license="cc-by",
        rerank_score=0.8,
        raw={"language": "en"},
    )
    patent = PatentResult(
        url="https://patents.google.com/patent/US1A1",
        title="Patent",
        content="patent abstract",
        source="patent_es",
        publication_number="US-1-A1",
        country="US",
        rerank_score=0.7,
    )
    evidence = EvidenceAssembler().assemble([web], [paper], [patent])
    ids_before = [item.id for item in evidence]

    retrieved_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    annotated = annotate_evidence(evidence, retrieved_at=retrieved_at)

    assert [item.id for item in annotated] == ids_before
    by_type = {item.type: item for item in annotated}

    web_evidence = by_type["web"]
    assert web_evidence.provenance is not None
    assert web_evidence.provenance.canonical_url == "https://example.com/news"
    assert web_evidence.provenance.publisher_id == "domain:example.com"
    assert web_evidence.provenance.retrieved_via == "tencent"
    assert web_evidence.provenance.content_origin == "provider_extract"
    assert web_evidence.provenance.retrieved_at == "2026-07-15T00:00:00Z"
    assert web_evidence.provenance.field_provenance["passage.text"].source_field == "content"
    assert web_evidence.quality is not None
    assert web_evidence.quality.level == "limited"
    assert "PROVIDER_EXTRACT_NOT_ORIGINAL" in web_evidence.quality.reasons

    academic = by_type["academic"]
    assert academic.provenance is not None
    assert academic.provenance.document_id == "W1"
    assert academic.provenance.version_id == "10.1000/example"
    assert academic.provenance.content_origin == "metadata"
    assert academic.provenance.license == "cc-by"
    assert academic.provenance.original_language == "en"
    assert academic.locator is not None
    assert academic.locator.section == "abstract"
    assert academic.quality is not None
    assert academic.quality.level == "discovery_only"
    assert academic.quality.can_support_key_claim is False
    assert "ABSTRACT_ONLY" in academic.diagnostics.warnings

    patent_evidence = by_type["patent"]
    assert patent_evidence.provenance is not None
    assert patent_evidence.provenance.publisher_id == "patent-authority:us"
    assert patent_evidence.provenance.version_id == "US-1-A1"
    assert patent_evidence.locator is not None
    assert patent_evidence.locator.section == "abstract"
    assert patent_evidence.quality is not None
    assert patent_evidence.quality.level == "discovery_only"
    assert "CLAIM_TEXT_UNAVAILABLE" in patent_evidence.diagnostics.warnings


def test_pdf_evidence_is_citable_only_with_stable_page_locator():
    with_pages = AcademicResult(
        url="https://doi.org/10.1/with-pages",
        title="Located PDF",
        source="openalex_local",
        work_id="W-pages",
        doi="10.1/with-pages",
        pdf_text="full text with pages",
        pdf_chunk_index=2,
        pdf_page_from=4,
        pdf_page_to=5,
        rerank_score=0.9,
    )
    without_pages = AcademicResult(
        url="https://doi.org/10.1/no-pages",
        title="Unlocated PDF",
        source="openalex_local",
        work_id="W-no-pages",
        doi="10.1/no-pages",
        pdf_text="full text without pages",
        rerank_score=0.8,
    )

    evidence = EvidenceAssembler().assemble([], [with_pages, without_pages], [])
    evidence = annotate_evidence(evidence)
    by_result = {item.result_id: item for item in evidence}

    located = by_result["academic:W-pages"]
    assert located.id == "academic:W-pages:pdf:2"
    assert located.locator is not None
    assert located.locator.page_from == 4
    assert located.locator.page_to == 5
    assert located.locator.chunk_index == 2
    assert located.quality is not None
    assert located.quality.level == "citable"
    assert located.quality.can_support_key_claim is True

    unlocated = by_result["academic:W-no-pages"]
    assert unlocated.quality is not None
    assert unlocated.quality.level == "limited"
    assert unlocated.quality.has_stable_locator is False
    assert "NO_STABLE_LOCATOR" in unlocated.diagnostics.warnings


def test_serpapi_snippet_is_discovery_only():
    result = SearchResult(
        url="https://example.org/result",
        title="Snippet",
        snippet="search snippet",
        content="search snippet",  # SerpAPI provider 的真实归一化行为
        source="serpapi",
        rerank_score=0.8,
    )

    evidence = EvidenceAssembler().assemble([result], [], [])
    evidence = annotate_evidence(evidence)

    assert evidence[0].provenance is not None
    assert evidence[0].provenance.content_origin == "snippet"
    assert evidence[0].quality is not None
    assert evidence[0].quality.level == "discovery_only"
    assert "SNIPPET_ONLY" in evidence[0].quality.reasons


def test_search_boundary_exposes_limits_and_serializes_to_json():
    paper = AcademicResult(
        url="https://doi.org/10.1/example",
        title="论文 Paper",
        content="abstract",
        source="openalex_local",
        work_id="W1",
        doi="10.1/example",
        license="cc-by",
        rerank_score=0.8,
    )
    evidence = EvidenceAssembler().assemble([], [paper], [])
    evidence = annotate_evidence(evidence)

    boundary = build_search_boundary(
        query="钠电池 battery paper",
        source_names=["openalex_local", "patent_es"],
        evidence=evidence,
        query_time=datetime(2026, 7, 15, tzinfo=timezone.utc),
        source_snapshot={
            "openalex_local": "service-index:unspecified",
            "patent_es": "index-alias:epo_docdb_read",
        },
        max_candidates=50,
    )
    data = boundary.model_dump(mode="json")

    assert boundary.languages == ["zh", "en"]
    assert boundary.jurisdictions == []
    assert boundary.license_scope == ["cc-by"]
    assert boundary.max_rounds == 1
    assert boundary.max_candidates == 50
    assert "NO_GLOBAL_DEADLINE" in boundary.limitations
    assert "SOURCE_SNAPSHOT_NOT_IMMUTABLE:patent_es" in boundary.limitations
    assert data["query_time"] == "2026-07-15T00:00:00Z"


def test_search_always_runs_basic_evidence_annotation(monkeypatch, empty_engine):

    def fail_if_called(*args, **kwargs):
        raise AssertionError("annotation called")

    monkeypatch.setattr("src.application.trust_annotator.annotate_evidence", fail_if_called)
    with pytest.raises(AssertionError, match="annotation called"):
        empty_engine.search("query", source_types=("web",))


def test_engine_defaults_to_annotate_mode(empty_engine):
    response = empty_engine.search("query", source_types=("web",))

    assert response.retrieval_boundary is not None
    assert response.schema_version == "search.v1"
    json.dumps(response.model_dump(mode="json"))


def test_engine_has_no_public_trust_mode(empty_engine):
    with pytest.raises(TypeError, match="trust_mode"):
        empty_engine.search("query", trust_mode="verify")
