from pathlib import Path

import pytest

from src.interfaces.presenters import McpSearchPresenter
from src.interfaces.schemas import SearchRequest, search_command_from_mapping
from src.models import Answerability, SearchFailure, SearchResponse


ROOT = Path(__file__).resolve().parents[1]


def test_rest_schema_maps_once_to_authoritative_search_command():
    request = SearchRequest(
        query="query",
        top_k=7,
        include_academic=True,
        ranking_profile="semantic",
        rewrite_enabled=True,
        trust_mode="off",
    )

    command = request.to_command()

    assert command.query == "query"
    assert command.top_k == 7
    assert command.include_academic is True
    assert command.ranking_profile == "semantic"
    assert command.rewrite_enabled is True
    assert command.trust_mode == "off"


def test_mcp_alias_mapping_uses_the_same_command_contract():
    command = search_command_from_mapping(
        {"query": "query", "rerank": False, "transport_local": "ignored"},
        aliases={"rerank": "rerank_enabled"},
    )

    assert command.query == "query"
    assert command.rerank_enabled is False
    assert not hasattr(command, "transport_local")


def test_mcp_presenter_has_versioned_lossless_contract():
    response = SearchResponse(
        query="raw",
        normalized_query="normalized",
        rewritten_query="rewritten",
        recency="month",
        time_sensitive=True,
        evidence=[],
        partial_failure=True,
        failures=[SearchFailure(stage="provider_search", code="FAILED")],
        answerability=Answerability(status="partial", confidence="low"),
        trust_mode="annotate",
        count=0,
        providers_used=["source"],
        reranker="semantic",
        ranking_profile="semantic",
        rerank_threshold=0.2,
        rerank_threshold_mode="strict",
        ranking_warnings=["warning"],
        elapsed_ms=42,
    )

    payload = McpSearchPresenter.present(response)
    restored = McpSearchPresenter.restore(payload)

    assert payload["schema_version"] == "mcp-search.v1"
    assert payload["meta"]["counts"] == {"web": 0, "academic": 0, "patent": 0}
    assert restored == response


def test_mcp_presenter_rejects_unknown_contract_version():
    with pytest.raises(ValueError, match="unsupported MCP search schema"):
        McpSearchPresenter.restore({"schema_version": "mcp-search.v2"})


def test_transports_and_tool_eval_share_use_case_and_presenter():
    api = (ROOT / "src" / "api.py").read_text()
    mcp = (ROOT / "src" / "mcp_server.py").read_text()
    tool_eval = (ROOT / "eval" / "run_tool_agent_eval.py").read_text()

    assert "engine.execute(" in api
    assert "engine.execute(" in mcp
    assert "engine.search(" not in api
    assert "engine.search(" not in mcp
    assert "McpSearchPresenter.restore(data)" in tool_eval
