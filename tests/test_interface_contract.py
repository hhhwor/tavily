from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.interfaces.presenters import McpSearchPresenter
from src.interfaces.schemas import SearchRequest
from src.domain.search_api import (
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
    assert restored == response


def test_mcp_presenter_rejects_unknown_contract_version():
    payload = McpSearchPresenter.present(_response())
    payload["schema_version"] = "search.v2"
    with pytest.raises(ValidationError):
        McpSearchPresenter.restore(payload)


def test_rest_and_mcp_share_search_and_research_use_cases():
    api = (ROOT / "src" / "api.py").read_text()
    mcp = (ROOT / "src" / "mcp_server.py").read_text()

    assert "engine.execute(" in api
    assert "engine.execute(" in mcp
    assert "engine.start_research(" in api
    assert "engine.start_research(" in mcp
    assert 'name="verify_claims"' not in mcp
    assert 'name="get_pdf_text"' not in mcp
