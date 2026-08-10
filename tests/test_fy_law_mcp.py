"""FY 法规 MCP adapter、垂直路由和配置契约。"""
from __future__ import annotations

import json

import pytest

from src.application.commands import SearchCommand
from src.application.evidence_assembler import EvidenceAssembler
from src.application.ports.retrieval import RetrievalRequest
from src.application.query_planner import QueryPlanner
from src.application.source_registry import SourceRegistry
from src.bootstrap import build_container
from src.config import Settings
from src.domain.errors import ExternalServiceError
from src.providers.fy_law_mcp import FyLawMcpProvider


class _Response:
    def __init__(self, payload, *, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, json, headers, timeout):
        self.calls.append({
            "url": url,
            "json": json,
            "headers": headers,
            "timeout": timeout,
        })
        return self.responses.pop(0)


def _law_record():
    return {
        "title": "中华人民共和国民法典",
        "content": "离婚后，不满两周岁的子女，以由母亲直接抚养为原则。",
        "law_type": "中央法规",
        "status": "现行有效",
        "department": "全国人民代表大会",
        "directory": ["第五编婚姻家庭", "第一千零八十四条"],
        "item": "第一千零八十四条",
        "score": 250.441,
    }


def _provider(http):
    return FyLawMcpProvider(
        endpoint="https://fy.example.test/354347/mcp_law_service",
        token="mcp-test-token",
        http_session=http,
    )


def test_provider_performs_streamable_http_handshake_and_normalizes_law():
    law = _law_record()
    http = _Http([
        _Response({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        _Response({}),
        _Response({
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": json.dumps(law)}]},
        }),
    ])

    results = _provider(http).search(
        "民法典第一千零八十四条子女抚养如何规定",
        top_k=3,
    )

    assert [item["json"]["method"] for item in http.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert all(
        item["headers"]["COP-FYOP-AUTHORIZATION"] == "mcp-test-token"
        for item in http.calls
    )
    assert all("Authorization" not in item["headers"] for item in http.calls)
    tool_call = http.calls[-1]["json"]
    assert tool_call["params"]["name"] == "flfg_iterative_search_tool"
    assert tool_call["params"]["arguments"] == {
        "query": {
            "title": "中华人民共和国民法典",
            "item": "第一千零八十四条",
            "content": "",
        },
        "status": "现行有效",
    }
    assert len(results) == 1
    result = results[0]
    assert result.url == ""
    assert result.title == "中华人民共和国民法典 第一千零八十四条"
    assert result.site == "中央法规 · 全国人民代表大会 · 现行有效"
    assert result.score == 250.441
    assert result.raw["directory"] == law["directory"]


def test_provider_rejects_jsonrpc_tool_errors_without_leaking_token():
    http = _Http([
        _Response({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        _Response({}),
        _Response({"jsonrpc": "2.0", "error": {"code": -32601}}),
    ])

    with pytest.raises(ExternalServiceError) as exc_info:
        _provider(http).search("劳动合同法")

    assert exc_info.value.code == "MCP_TOOL_REJECTED"
    assert "mcp-test-token" not in str(exc_info.value)


def test_provider_passes_requested_legal_status_to_fy_tool():
    http = _Http([
        _Response({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        _Response({}),
        _Response({
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": json.dumps(_law_record())}]},
        }),
    ])

    results = _provider(http).search_request(RetrievalRequest(
        query="民法典第一千零八十四条",
        candidate_budget=3,
        legal_status="失效",
    ))

    assert len(results) == 1
    assert http.calls[-1]["json"]["params"]["arguments"]["status"] == "失效"


def test_provider_exposes_law_metadata_as_public_evidence():
    http = _Http([
        _Response({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        _Response({}),
        _Response({
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": json.dumps(_law_record())}]},
        }),
    ])

    results = _provider(http).search("民法典第一千零八十四条")
    evidence = EvidenceAssembler().assemble(results, (), ())

    assert evidence[0].legal is not None
    assert evidence[0].legal.model_dump() == {
        "law_type": "中央法规",
        "status": "现行有效",
        "department": "全国人民代表大会",
        "directory": ["第五编婚姻家庭", "第一千零八十四条"],
        "item": "第一千零八十四条",
    }


def test_provider_reloads_token_file_for_each_search_and_reports_anonymous_status(
    tmp_path,
):
    token_path = tmp_path / "fy-law-mcp.token"
    token_path.write_text("first-token", encoding="utf-8")
    http = _Http([
        _Response({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        _Response({}),
        _Response({
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": json.dumps(_law_record())}]},
        }),
        _Response({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-03-26"}}),
        _Response({}),
        _Response({
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": json.dumps(_law_record())}]},
        }),
    ])
    provider = FyLawMcpProvider(
        endpoint="https://fy.example.test/354347/mcp_law_service",
        token="",
        token_file=str(token_path),
        http_session=http,
    )

    provider.search("民法典")
    token_path.write_text("rotated-token", encoding="utf-8")
    provider.search("民法典")

    assert [
        item["headers"]["COP-FYOP-AUTHORIZATION"] for item in http.calls[:3]
    ] == ["first-token"] * 3
    assert [
        item["headers"]["COP-FYOP-AUTHORIZATION"] for item in http.calls[3:]
    ] == ["rotated-token"] * 3
    status = provider.runtime_status()
    assert status["token_source"] == "file"
    assert status["last_success_at"] is not None
    assert status["last_result_count"] == 1
    assert "first-token" not in json.dumps(status)
    assert "rotated-token" not in json.dumps(status)
    assert str(token_path) not in json.dumps(status)


def test_registry_keeps_legal_provider_out_of_default_web_route():
    provider = _provider(_Http([]))
    registry = SourceRegistry([provider])

    assert registry.ids_for_verticals("web") == ()
    assert registry.ids_for_verticals("web", ("legal",)) == ("fy_law_mcp",)
    assert registry.has_vertical("web", "legal") is True


def test_legal_vertical_uses_only_legal_web_provider_and_disables_auto_other_domains():
    planner = QueryPlanner(Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
    ))

    planned = planner.plan(
        SearchCommand("民法典子女抚养", verticals=("legal",)),
        ("fy_law_mcp",),
        academic_available=False,
        patent_available=False,
        legal_available=True,
    )

    assert planned.active_provider_names == ("fy_law_mcp",)
    assert planned.do_academic is False
    assert planned.do_patent is False


def test_missing_legal_provider_has_a_routing_failure():
    planner = QueryPlanner(Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
    ))

    planned = planner.plan(
        SearchCommand("民法典", verticals=("legal",)),
        (),
        academic_available=False,
        patent_available=False,
        legal_available=False,
    )

    assert planned.active_provider_names == ()
    assert [item.code for item in planned.failures] == ["PROVIDER_UNAVAILABLE"]


def test_legal_settings_are_secret_safe_and_required_when_enabled():
    configured = Settings.from_env({
        "FY_LAW_MCP_ENABLED": "true",
        "FY_LAW_MCP_TOKEN": "mcp-test-token",
        "OPENALEX_ENABLED": "false",
    })

    assert configured.fy_law_mcp_enabled is True
    assert configured.fy_law_mcp_url.endswith("/mcp_law_service")
    assert configured.enabled_providers == ("fy_law_mcp",)
    assert "mcp-test-token" not in repr(configured)
    file_configured = Settings.from_env({
        "FY_LAW_MCP_ENABLED": "true",
        "FY_LAW_MCP_TOKEN_FILE": "/run/secrets/fy-law-mcp.token",
        "OPENALEX_ENABLED": "false",
    })
    assert file_configured.fy_law_mcp_token == ""
    assert file_configured.fy_law_mcp_token_file == "/run/secrets/fy-law-mcp.token"
    with pytest.raises(ValueError, match="FY_LAW_MCP_TOKEN"):
        Settings.from_env({"FY_LAW_MCP_ENABLED": "true"})


def test_container_registers_only_the_legal_route_when_enabled():
    container = build_container(
        Settings(
            openalex_enabled=False,
            patent_es_enabled=False,
            ranking_profile="fast",
            rerank_threshold_mode="off",
            mcp_mode="false",
            state_db_path=":memory:",
            fy_law_mcp_enabled=True,
            fy_law_mcp_token="mcp-test-token",
        ),
        include_mcp=False,
    )
    try:
        assert [item.descriptor.id for item in container.engine.providers] == [
            "fy_law_mcp"
        ]
        assert container.engine.source_registry.ids_for_verticals("web") == ()
        assert container.engine.source_registry.ids_for_verticals(
            "web", ("legal",)
        ) == ("fy_law_mcp",)
    finally:
        container.close()
