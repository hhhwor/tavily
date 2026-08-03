import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.interfaces.presenters import McpSearchPresenter
from src.interfaces.public_models import public_research_response
from src.interfaces.schemas import SearchRequest
from src.engine import SearchEngine
from src.domain.evidence import (
    AnswerabilityGap,
    Evidence,
    EvidenceFieldProvenance,
    EvidencePassage,
    EvidenceProvenance,
    SearchBoundary,
)
from src.domain.failures import SearchFailure
from src.domain.research import (
    AssessmentDimension,
    EvidenceFunnel,
    ResearchAssessment,
    ResearchCoverage,
    ResearchDossier,
    ResearchLinks,
    ResearchTaskEnvelope,
)
from src.domain.search_api import (
    FailureDetail,
    QualityMix,
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchMeta,
    SearchQuery,
    SearchResponse,
    SearchResultSet,
)


ROOT = Path(__file__).resolve().parents[1]


def test_rest_schema_maps_once_to_authoritative_search_command():
    request = SearchRequest.model_validate({
        "query": "query",
        "limit": 7,
        "source_types": ["academic", "patent"],
        "filters": {
            "published_from": "2024-01-01",
            "languages": ["zh", "en"],
            "jurisdictions": ["CN"],
        },
    })

    command = request.to_command()

    assert command.query == "query"
    assert command.limit == 7
    assert command.source_types == ("academic", "patent")
    assert command.filters.languages == ("zh", "en")
    assert command.filters.jurisdictions == ("CN",)


def test_search_request_is_strict_and_has_no_execution_tuning_fields():
    schema = SearchRequest.model_json_schema()["properties"]
    assert set(schema) == {"query", "limit", "source_types", "filters"}
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "q", "top_k": 3})


def _response() -> SearchResponse:
    now = datetime.now(timezone.utc)
    return SearchResponse(
        request_id="req_test",
        status="complete",
        research_seed=None,
        query=SearchQuery(
            original="raw",
            effective="normalized",
            filters_requested=RequestedFilters(),
        ),
        result_set=SearchResultSet(returned=0, limit=10),
        retrieval_assessment=RetrievalAssessment(
            status="unusable",
            quality_mix=QualityMix(),
        ),
        retrieval_boundary=RetrievalBoundary(
            query_time=now,
            deadline_ms=2000,
        ),
        meta=SearchMeta(elapsed_ms=42),
    )


def test_mcp_presenter_is_lossless_search_v1_identity_projection():
    response = _response()
    payload = McpSearchPresenter.present(response)
    restored = McpSearchPresenter.restore(payload)

    assert payload["schema_version"] == "search.v1"
    assert payload["result_set"]["counts_by_stage"] == {
        "recalled": {"web": 0, "academic": 0, "patent": 0},
        "ranked": {"web": 0, "academic": 0, "patent": 0},
        "assembled": {"web": 0, "academic": 0, "patent": 0},
        "selected": {"web": 0, "academic": 0, "patent": 0},
    }
    assert restored == response


def test_search_failure_exposes_machine_readable_degradation():
    response = _response().model_copy(update={
        "status": "partial",
        "failures": [
            FailureDetail(
                stage="rerank",
                source="web_reranker",
                code="RERANK_TIMEOUT",
                retryable=True,
                degradation={
                    "action": "use_unreranked_results",
                    "impact": "quality",
                    "retry_owner": "server",
                },
            )
        ],
    })

    payload = McpSearchPresenter.present(response)

    assert payload["failures"][0]["degradation"] == {
        "action": "use_unreranked_results",
        "impact": "quality",
        "retry_owner": "server",
    }


def test_mcp_presenter_rejects_unknown_contract_version():
    payload = McpSearchPresenter.present(_response())
    payload["schema_version"] = "search.v2"
    with pytest.raises(ValidationError):
        McpSearchPresenter.restore(payload)


def _provider_evidence() -> Evidence:
    return Evidence(
        id="web:test:content",
        result_id="web:test",
        type="web",
        source="doubao",
        title="Result",
        url="https://example.test/result",
        passage=EvidencePassage(text="evidence"),
        provenance=EvidenceProvenance(
            canonical_url="https://example.test/result",
            publisher_name="Example Publisher",
            retrieved_via="doubao",
            retrieved_at="2026-08-03T00:00:00Z",
            field_provenance={
                "passage.text": EvidenceFieldProvenance(
                    source_field="content",
                    retrieved_via="doubao",
                )
            },
        ),
    )


def test_public_search_output_hides_provider_attribution_without_mutating_internal_data():
    response = _response().model_copy(update={
        "evidence": [_provider_evidence()],
        "retrieval_assessment": RetrievalAssessment(
            gaps=[AnswerabilityGap(
                code="PARTIAL_FAILURE",
                message="aliyun provider failed",
                source="aliyun",
            )],
        ),
        "retrieval_boundary": RetrievalBoundary(
            query_time=datetime.now(timezone.utc),
            deadline_ms=2000,
            source_snapshot={"doubao": "mcp-server:test"},
            limitations=["SOURCE_SNAPSHOT_NOT_IMMUTABLE:doubao"],
        ),
        "failures": [FailureDetail(
            stage="provider_search",
            source="aliyun",
            code="PROVIDER_SEARCH_FAILED",
            message="aliyun external service failed",
        )],
    })

    payload = McpSearchPresenter.present(response)

    assert response.evidence[0].source == "doubao"
    assert response.retrieval_boundary.source_snapshot == {
        "doubao": "mcp-server:test"
    }
    assert "source" not in payload["evidence"][0]
    assert "retrieved_via" not in payload["evidence"][0]["provenance"]
    assert "retrieved_via" not in (
        payload["evidence"][0]["provenance"]["field_provenance"]["passage.text"]
    )
    assert "source_snapshot" not in payload["retrieval_boundary"]
    assert "source" not in payload["retrieval_assessment"]["gaps"][0]
    assert "source" not in payload["failures"][0]
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert "doubao" not in serialized
    assert "aliyun" not in serialized

    class SearchService:
        def execute(self, command):
            return response

    engine = SearchEngine(
        settings=None,
        search_service=SearchService(),
        research_service=None,
        providers=[],
    )
    engine_response = engine.execute(object())
    assert engine_response.evidence[0].source == ""
    assert engine_response.retrieval_boundary.source_snapshot == {}
    assert response.evidence[0].source == "doubao"


def test_public_research_output_hides_provider_attribution():
    now = datetime.now(timezone.utc)
    dimension = AssessmentDimension(status="insufficient")
    response = ResearchTaskEnvelope(
        research_id="res_test",
        state="completed",
        seed_search_id="srch_test",
        seed_snapshot_hash="sha256:test",
        created_at=now,
        updated_at=now,
        dossier=ResearchDossier(
            assessment=ResearchAssessment(
                coverage=dimension,
                independence=dimension,
                locatability=dimension,
                consistency=dimension,
                source_quality=dimension,
                reproducibility=dimension,
            ),
            evidence_funnel=EvidenceFunnel(),
            coverage=ResearchCoverage(),
            boundaries=SearchBoundary(
                query_time="2026-08-03T00:00:00Z",
                source_snapshot={"doubao": "mcp-server:test"},
                limitations=["SOURCE_SNAPSHOT_NOT_IMMUTABLE:doubao"],
            ),
            evidence_index={"web:test:content": _provider_evidence()},
        ),
        failures=[SearchFailure(
            stage="provider_search",
            source="aliyun",
            code="PROVIDER_SEARCH_FAILED",
            message="aliyun external service failed",
        )],
        links=ResearchLinks(
            self="/research/res_test",
            feedback="/research/res_test/feedback",
            cancel="/research/res_test/cancel",
        ),
    )

    payload = public_research_response(response).model_dump(mode="json")

    assert response.dossier.evidence_index["web:test:content"].source == "doubao"
    assert "source" not in payload["dossier"]["evidence_index"]["web:test:content"]
    assert "source_snapshot" not in payload["dossier"]["boundaries"]
    assert "source" not in payload["failures"][0]
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert "doubao" not in serialized
    assert "aliyun" not in serialized


def test_rest_and_mcp_share_search_and_research_use_cases():
    api = (ROOT / "src" / "api.py").read_text()
    mcp = (ROOT / "src" / "mcp_server.py").read_text()

    assert "engine.execute(" in api
    assert "engine.execute(" in mcp
    assert "engine.start_research(" in api
    assert "engine.start_research(" in mcp
    assert 'name="verify_claims"' not in mcp
    assert 'name="get_pdf_text"' not in mcp
