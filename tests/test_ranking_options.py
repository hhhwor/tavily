"""F01 排序选项解析与 REST 边界契约。"""
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pipeline.ranking_options import resolve_ranking_options


def _resolve(**overrides):
    return resolve_ranking_options(
        default_profile="quality",
        default_threshold=0.3,
        default_threshold_mode="prefer",
        **overrides,
    )


def test_defaults_preserve_current_quality_behavior():
    options = _resolve()
    assert options.profile == "quality"
    assert options.threshold == 0.3
    assert options.threshold_mode == "prefer"
    assert options.warnings == ()


@pytest.mark.parametrize(
    ("overrides", "profile"),
    [
        ({"fusion_enabled": True}, "quality"),
        ({"fusion_enabled": False}, "semantic"),
        ({"rerank_enabled": False}, "fast"),
        ({"rerank_enabled": True}, "quality"),
        ({"rerank_backend": "none"}, "fast"),
    ],
)
def test_legacy_options_map_to_canonical_profiles(overrides, profile):
    assert _resolve(**overrides).profile == profile


def test_legacy_fusion_is_ignored_when_rerank_is_disabled():
    options = _resolve(rerank_enabled=False, fusion_enabled=True)
    assert options.profile == "fast"
    assert "FUSION_IGNORED_FAST_PROFILE" in options.warnings


@pytest.mark.parametrize(
    "overrides",
    [
        {"ranking_profile": "fast", "rerank_enabled": True},
        {"ranking_profile": "quality", "rerank_enabled": False},
        {"ranking_profile": "quality", "fusion_enabled": False},
        {"ranking_profile": "semantic", "fusion_enabled": True},
        {"ranking_profile": "quality", "rerank_backend": "none"},
        {"ranking_profile": "unknown"},
        {"rerank_threshold_mode": "unknown"},
    ],
)
def test_conflicting_or_unknown_options_are_rejected(overrides):
    with pytest.raises(ValueError):
        _resolve(**overrides)


def test_fast_profile_disables_threshold_without_failing_request():
    options = _resolve(
        ranking_profile="fast",
        rerank_threshold=0.8,
        rerank_threshold_mode="strict",
    )
    assert options.threshold == 0.8
    assert options.threshold_mode == "off"
    assert "THRESHOLD_SKIPPED_NO_SCORER" in options.warnings


def test_zero_threshold_is_effectively_off():
    options = _resolve(rerank_threshold=0, rerank_threshold_mode="strict")
    assert options.threshold == 0
    assert options.threshold_mode == "off"


def test_settings_do_not_treat_unset_legacy_false_as_semantic():
    from src.config import Settings

    configured = Settings.from_env({})

    assert configured.ranking_profile == "quality"
    assert configured.rerank_threshold_mode == "prefer"
    assert configured.rerank_enabled is True
    assert configured.fusion_enabled is True


def test_engine_noop_disables_strict_threshold_without_partial_failure(monkeypatch):
    from src.engine import SearchEngine
    from src.config import Settings
    from src.pipeline.rerank import NoOpReranker

    engine = object.__new__(SearchEngine)
    engine.providers = []
    engine.academic_provider = None
    engine.patent_provider = None
    engine.cache = None
    engine.text_scorer = NoOpReranker()
    engine._text_scorer_cache = {}
    engine.settings = Settings()
    engine._http = None
    monkeypatch.setattr(
        engine,
        "_select_text_scorer",
        lambda *args, **kwargs: NoOpReranker(),
    )

    response = engine.search(
        "query",
        include_academic=False,
        include_patent=False,
        ranking_profile="quality",
        rerank_backend="siliconflow",
        rerank_threshold_mode="strict",
        rewrite_enabled=False,
        trust_mode="off",
    )

    assert response.ranking_profile == "quality"
    assert response.rerank_threshold_mode == "off"
    assert response.ranking_warnings == ["THRESHOLD_SKIPPED_NO_SCORER"]
    assert response.partial_failure is False


def test_rest_request_reports_profile_conflicts_as_validation_error():
    from src.api import SearchRequest

    with pytest.raises(ValidationError):
        SearchRequest.model_validate({
            "query": "test",
            "ranking_profile": "quality",
            "fusion_enabled": False,
        })
