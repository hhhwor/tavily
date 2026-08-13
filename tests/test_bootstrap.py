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
from src.providers.doubao import DoubaoSearchProvider


def _safe_settings(**overrides) -> Settings:
    values = {
        "openalex_enabled": False,
        "patent_es_enabled": False,
        "ranking_profile": "fast",
        "rerank_threshold_mode": "off",
        "mcp_mode": "false",
        "state_db_path": ":memory:",
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
assert '/research' in src.api.app.openapi()['paths']
assert '/verify' not in src.api.app.openapi()['paths']
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


def test_qwen3_0_6b_is_the_default_rerank_model():
    expected = "Qwen/Qwen3-Reranker-0.6B"

    assert Settings().rerank_model == expected
    assert Settings.from_env({}).rerank_model == expected


def test_intent_classifier_has_opt_in_embedding_defaults_and_validates_key():
    defaults = Settings.from_env({})
    configured = Settings.from_env({
        "SILICONFLOW_API_KEY": "test-key",
        "INTENT_CLASSIFIER_ENABLED": "true",
        "INTENT_CLASSIFIER_MIN_CONFIDENCE": "0.75",
    })

    assert defaults.intent_classifier_enabled is False
    assert defaults.intent_classifier_backend == "embedding"
    assert defaults.intent_classifier_model == "Qwen/Qwen3-Embedding-0.6B"
    assert configured.intent_classifier_enabled is True
    assert configured.intent_classifier_min_confidence == 0.75
    with pytest.raises(ValueError, match="SILICONFLOW_API_KEY"):
        Settings.from_env({"INTENT_CLASSIFIER_ENABLED": "true"})
    with pytest.raises(ValueError, match="INTENT_CLASSIFIER_MIN_CONFIDENCE"):
        Settings.from_env({"INTENT_CLASSIFIER_MIN_CONFIDENCE": "1.01"})
    with pytest.raises(ValueError, match="INTENT_CLASSIFIER_BACKEND"):
        Settings.from_env({"INTENT_CLASSIFIER_BACKEND": "unknown"})
    with pytest.raises(ValueError, match=r"INTENT_EMBEDDING_\*_THRESHOLD"):
        Settings.from_env({"INTENT_EMBEDDING_PATENT_THRESHOLD": "1.01"})

    chat = Settings.from_env({"INTENT_CLASSIFIER_BACKEND": "chat"})
    assert chat.intent_classifier_model == "Qwen/Qwen3-8B"


def test_claim_verifier_defaults_to_structured_qwen3_model():
    defaults = Settings.from_env({})
    overridden = Settings.from_env({
        "TRUST_VERIFY_MODEL": "vendor/custom-verifier",
    })

    assert Settings().trust_verify_model == "Qwen/Qwen3-8B"
    assert defaults.trust_verify_model == "Qwen/Qwen3-8B"
    assert overridden.trust_verify_model == "vendor/custom-verifier"


def test_doubao_credentials_enable_provider_without_leaking_from_repr():
    configured = Settings.from_env({
        "ASK_ECHO_SEARCH_INFINITY_API_KEY": "doubao-test-key",
        "DOUBAO_UVX_PATH": "/opt/bin/uvx",
        "OPENALEX_ENABLED": "false",
    })

    assert configured.doubao_api_key == "doubao-test-key"
    assert configured.doubao_uvx_path == "/opt/bin/uvx"
    assert configured.enabled_providers == ("doubao",)
    assert "doubao-test-key" not in repr(configured)


def test_doubao_api_key_prefers_new_environment_name():
    configured = Settings.from_env({
        "DOUBAO_API_KEY": "new-key",
        "ASK_ECHO_SEARCH_INFINITY_API_KEY": "legacy-key",
        "OPENALEX_ENABLED": "false",
    })

    assert configured.doubao_api_key == "new-key"
    assert "new-key" not in repr(configured)


def test_serpapi_requires_explicit_opt_in():
    disabled = Settings.from_env({
        "SERPAPI_API_KEY": "serpapi-test-key",
        "OPENALEX_ENABLED": "false",
    })
    enabled = Settings.from_env({
        "SERPAPI_API_KEY": "serpapi-test-key",
        "SERPAPI_ENABLED": "true",
        "OPENALEX_ENABLED": "false",
    })

    assert disabled.enabled_providers == ()
    assert enabled.enabled_providers == ("serpapi",)
    with pytest.raises(ValueError, match="SERPAPI_API_KEY"):
        Settings.from_env({"SERPAPI_ENABLED": "true"})


def test_resilience_settings_are_explicit_and_validated():
    configured = Settings.from_env({
        "OPENALEX_ENABLED": "false",
        "PATENT_ES_ENABLED": "false",
        "RESILIENCE_MAX_ATTEMPTS": "3",
        "RESILIENCE_BACKOFF_BASE_MS": "50",
        "RESILIENCE_BACKOFF_MAX_MS": "500",
        "CIRCUIT_FAILURE_THRESHOLD": "4",
        "CIRCUIT_OPEN_SECONDS": "20",
    })

    assert configured.resilience_max_attempts == 3
    assert configured.resilience_backoff_base_ms == 50
    assert configured.resilience_backoff_max_ms == 500
    assert configured.circuit_failure_threshold == 4
    assert configured.circuit_open_seconds == 20
    with pytest.raises(ValueError, match="RESILIENCE_MAX_ATTEMPTS"):
        Settings.from_env({"RESILIENCE_MAX_ATTEMPTS": "0"})


def test_vertical_provider_flags_have_explicit_tristate_semantics():
    assert Settings.from_env({"OPENALEX_ENABLED": "false"}).academic_enabled is False
    assert Settings.from_env({"PATENT_ES_URL": "https://example.invalid"}).patent_enabled is True
    with pytest.raises(ValueError, match="PATENT_ES_URL"):
        Settings.from_env({"PATENT_ES_ENABLED": "true"})
    fulltext = Settings.from_env({
        "PATENT_FULLTEXT_URL": "https://fulltext.example.invalid",
    })
    assert fulltext.patent_fulltext_enabled is True
    assert fulltext.patent_fulltext_index == "epo_fulltext_read"
    with pytest.raises(ValueError, match="PATENT_FULLTEXT_URL"):
        Settings.from_env({"PATENT_FULLTEXT_ENABLED": "true"})
    with pytest.raises(ValueError, match="PATENT_FULLTEXT_INDEX"):
        Settings.from_env({
            "PATENT_FULLTEXT_ENABLED": "true",
            "PATENT_FULLTEXT_URL": "https://fulltext.example.invalid",
            "PATENT_FULLTEXT_INDEX": "",
        })


def test_provider_does_not_fall_back_to_process_environment(monkeypatch):
    monkeypatch.setenv("QIANFAN_API_KEY", "must-not-be-read")
    with pytest.raises(ValueError, match="QIANFAN_API_KEY"):
        BaiduSearchProvider(api_key="")

    monkeypatch.setenv(
        "ASK_ECHO_SEARCH_INFINITY_API_KEY",
        "must-not-be-read",
    )
    with pytest.raises(ValueError, match="ASK_ECHO_SEARCH_INFINITY_API_KEY"):
        DoubaoSearchProvider(api_key="")


def test_container_registers_doubao_as_a_web_provider():
    container = build_container(
        _safe_settings(
            doubao_api_key="test-key",
            doubao_uvx_path="/bin/true",
        ),
        include_mcp=False,
    )
    try:
        assert [provider.descriptor.id for provider in container.engine.providers] == [
            "doubao"
        ]
        assert container.engine.source_registry.ids("web") == ("doubao",)
    finally:
        container.close()


def test_container_excludes_serpapi_until_explicitly_enabled():
    disabled = build_container(
        _safe_settings(serpapi_api_key="test-key"),
        include_mcp=False,
    )
    enabled = build_container(
        _safe_settings(
            serpapi_api_key="test-key",
            serpapi_enabled=True,
        ),
        include_mcp=False,
    )
    try:
        assert disabled.engine.providers == []
        assert [
            provider.descriptor.id for provider in enabled.engine.providers
        ] == ["serpapi"]
    finally:
        disabled.close()
        enabled.close()


def test_container_registers_aliyun_by_default_and_supports_kill_switch():
    credentials = {
        "aliyun_access_key_id": "test-id",
        "aliyun_access_key_secret": "test-secret",
    }
    enabled = build_container(
        _safe_settings(**credentials),
        include_mcp=False,
    )
    disabled = build_container(
        _safe_settings(
            **credentials,
            aliyun_web_search_enabled=False,
        ),
        include_mcp=False,
    )
    try:
        assert disabled.engine.providers == []
        assert [
            provider.descriptor.id for provider in enabled.engine.providers
        ] == ["aliyun"]
        assert enabled.engine.providers[0]._http is enabled.http_session
    finally:
        disabled.close()
        enabled.close()


def test_container_injects_shared_session_and_isolated_executors():
    container = build_container(
        _safe_settings(qianfan_api_key="test-key"),
        include_mcp=False,
    )
    try:
        service = container.engine._search_service
        discovery = service._discovery
        assert discovery._recall._executor is container.recall_executor
        assert discovery._ranking._executor is container.ranking_executor
        pdf = container.engine._research_service._pdf_gateway
        assert pdf._executor is container.pdf_executor
        assert len({
            id(container.recall_executor),
            id(container.ranking_executor),
            id(container.pdf_executor),
        }) == 3
        assert container.executor is container.recall_executor
        assert discovery._query_planner._rewriter._http is container.http_session
        assert pdf._http is container.http_session
        assert container.engine.providers[0]._http is container.http_session
        assert "test-key" not in repr(container.settings)
    finally:
        container.close()

    assert container.recall_executor._shutdown is True
    assert container.ranking_executor._shutdown is True
    assert container.pdf_executor._shutdown is True


def test_container_injects_enabled_intent_classifier_with_shared_session():
    container = build_container(
        _safe_settings(
            siliconflow_api_key="test-key",
            intent_classifier_enabled=True,
        ),
        include_mcp=False,
    )
    try:
        classifier = container.engine._search_service._discovery._query_planner._intent_classifier
        assert classifier is not None
        assert classifier._http is container.http_session
        assert classifier._model == "Qwen/Qwen3-Embedding-0.6B"
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
        assert health.json()["research_verifier_availability"] == {
            "status": "available",
            "backend": "rules",
            "model": "rules:v1",
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure_codes": [],
        }
        assert health.json()["resilience"] == {
            "max_attempts": 2,
            "dependencies": {},
        }
        assert client.post("/mcp").status_code == 404
        response = client.post(
            "/search",
            json={
                "query": "test",
                "source_types": ["web"],
            },
        )
        assert response.status_code == 200
        assert response.json()["schema_version"] == "search.v1"
        assert response.json()["research_seed"]["search_id"].startswith("srch_")

    assert created[0].closed is True


def test_explicit_container_is_rejected_after_it_has_closed():
    container = build_container(_safe_settings(), include_mcp=False)
    container.close()

    with pytest.raises(RuntimeError, match="Container 已关闭"):
        with TestClient(create_app(container)):
            pass


def test_search_rejects_removed_ranking_controls():
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
            },
        )
    assert response.status_code == 422
    assert "Extra inputs are not permitted" in response.text


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
    assert "Extra inputs are not permitted" in response.text


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
