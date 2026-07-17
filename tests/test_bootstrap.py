"""F02 composition root、不可变配置与应用生命周期契约。"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.api import SearchRequest, create_app
from src.bootstrap import build_container
from src.config import Settings
from src.providers.baidu import BaiduSearchProvider


def _safe_settings(**overrides) -> Settings:
    values = {
        "openalex_enabled": False,
        "patent_es_enabled": False,
        "ranking_profile": "fast",
        "rerank_threshold_mode": "off",
        "mcp_mode": "false",
    }
    values.update(overrides)
    return Settings(**values)


def test_importing_api_does_not_parse_env_or_build_runtime():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["SEARCH_TOP_K"] = "not-an-integer"
    code = """
import src.engine
src.engine.SearchEngine.__init__ = lambda *a, **k: (_ for _ in ()).throw(AssertionError('built'))
import src.mcp_server
src.mcp_server.build_mcp = lambda *a, **k: (_ for _ in ()).throw(AssertionError('mcp built'))
import src.api
assert '/search' in src.api.app.openapi()['paths']
assert '/verify' in src.api.app.openapi()['paths']
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_settings_from_env_is_frozen_and_does_not_mutate_process_env(monkeypatch):
    monkeypatch.delenv("F02_SENTINEL", raising=False)
    configured = Settings.from_env(
        {
            "F02_SENTINEL": "from-mapping",
            "OPENALEX_ENABLED": "false",
            "PATENT_ES_ENABLED": "false",
            "MCP_ENABLED": "false",
        }
    )

    assert "F02_SENTINEL" not in os.environ
    assert configured.academic_enabled is False
    assert configured.patent_enabled is False
    assert configured.mcp_enabled is False
    with pytest.raises(FrozenInstanceError):
        configured.default_top_k = 20  # type: ignore[misc]


def test_vertical_provider_flags_have_explicit_tristate_semantics():
    assert Settings.from_env({"OPENALEX_ENABLED": "false"}).academic_enabled is False
    assert Settings.from_env({"PATENT_ES_URL": "https://example.invalid"}).patent_enabled is True
    with pytest.raises(ValueError, match="PATENT_ES_URL"):
        Settings.from_env({"PATENT_ES_ENABLED": "true"})


def test_provider_does_not_fall_back_to_process_environment(monkeypatch):
    monkeypatch.setenv("QIANFAN_API_KEY", "must-not-be-read")
    with pytest.raises(ValueError, match="QIANFAN_API_KEY"):
        BaiduSearchProvider(api_key="")


def test_container_injects_shared_session_and_executor():
    container = build_container(
        _safe_settings(qianfan_api_key="test-key"),
        include_mcp=False,
    )
    try:
        service = container.engine._search_service
        assert service._recall._executor is container.executor
        assert service._ranking._executor is container.executor
        assert service._pdf_gateway._executor is container.executor
        assert service._query_planner._rewriter._http is container.http_session
        assert service._pdf_gateway._http is container.http_session
        assert container.engine.providers[0]._http is container.http_session
        assert "test-key" not in repr(container.settings)
    finally:
        container.close()


def test_create_app_defers_factory_and_closes_runtime():
    calls = []
    created = []

    def factory():
        calls.append("factory")
        container = build_container(_safe_settings())
        created.append(container)
        return container

    application = create_app(container_factory=factory)
    assert calls == []
    assert "/search" in application.openapi()["paths"]
    assert calls == []

    with TestClient(application) as client:
        assert calls == ["factory"]
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["mcp"] is False
        assert client.post("/mcp").status_code == 404
        response = client.post(
            "/search",
            json={
                "query": "test",
                "include_academic": False,
                "include_patent": False,
                "trust_mode": "off",
            },
        )
        assert response.status_code == 200
        assert response.json()["ranking_profile"] == "fast"

    assert created[0].closed is True


def test_explicit_container_is_rejected_after_it_has_closed():
    container = build_container(_safe_settings(), include_mcp=False)
    container.close()

    with pytest.raises(RuntimeError, match="Container 已关闭"):
        with TestClient(create_app(container)):
            pass


def test_server_default_ranking_conflict_is_rest_422():
    container = build_container(
        _safe_settings(rerank_backend="none"),
        include_mcp=False,
    )
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/search",
            json={
                "query": "test",
                "ranking_profile": "quality",
                "include_academic": False,
                "include_patent": False,
            },
        )
    assert response.status_code == 422
    assert "rerank_backend=none" in response.text


@pytest.mark.parametrize("field", ["rerank_backend", "rerank_model"])
def test_rest_rejects_request_level_model_selection(field):
    schema = SearchRequest.model_json_schema()
    assert field not in schema["properties"]

    container = build_container(_safe_settings(), include_mcp=False)
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/search",
            json={
                "query": "test",
                field: "attacker-controlled-model",
            },
        )

    assert response.status_code == 422
    assert "ranking_profile" in response.text


def test_invalid_numeric_config_fails_only_when_explicitly_parsed():
    with pytest.raises(ValueError, match="SEARCH_TOP_K"):
        Settings.from_env({"SEARCH_TOP_K": "invalid"})


@pytest.mark.parametrize(
    ("value", "enabled", "required"),
    [("auto", True, False), ("true", True, True), ("false", False, False)],
)
def test_mcp_mode_is_explicit(value, enabled, required):
    configured = Settings.from_env({"MCP_ENABLED": value})
    assert configured.mcp_enabled is enabled
    assert configured.mcp_required is required
