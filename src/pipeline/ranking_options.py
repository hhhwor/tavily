"""Canonical ranking options and compatibility mapping for legacy switches."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal, Optional, Tuple


RankingProfile = Literal["fast", "semantic", "quality"]
ThresholdMode = Literal["off", "prefer", "strict"]

RANKING_PROFILES = ("fast", "semantic", "quality")
THRESHOLD_MODES = ("off", "prefer", "strict")


def parse_ranking_profile(value: str) -> RankingProfile:
    profile = (value or "").strip().lower()
    if profile not in RANKING_PROFILES:
        raise ValueError(
            f"ranking_profile 仅支持 {', '.join(RANKING_PROFILES)}，收到 {value!r}"
        )
    return profile  # type: ignore[return-value]


def parse_threshold_mode(value: str) -> ThresholdMode:
    mode = (value or "").strip().lower()
    if mode not in THRESHOLD_MODES:
        raise ValueError(
            f"rerank_threshold_mode 仅支持 {', '.join(THRESHOLD_MODES)}，收到 {value!r}"
        )
    return mode  # type: ignore[return-value]


def _with_warning(warnings: Tuple[str, ...], warning: str) -> Tuple[str, ...]:
    if not warning or warning in warnings:
        return warnings
    return (*warnings, warning)


@dataclass(frozen=True)
class RankingOptions:
    """Effective per-request ranking behavior after resolving legacy aliases."""

    profile: RankingProfile
    threshold: float
    threshold_mode: ThresholdMode
    warnings: Tuple[str, ...] = ()

    @property
    def text_scoring_enabled(self) -> bool:
        return self.profile != "fast"

    @property
    def fusion_enabled(self) -> bool:
        return self.profile == "quality"

    def disable_threshold(self, warning: str) -> "RankingOptions":
        return replace(
            self,
            threshold_mode="off",
            warnings=_with_warning(self.warnings, warning),
        )


def resolve_ranking_options(
    *,
    default_profile: str,
    default_threshold: float,
    default_threshold_mode: str,
    ranking_profile: Optional[str] = None,
    rerank_enabled: Optional[bool] = None,
    fusion_enabled: Optional[bool] = None,
    rerank_backend: Optional[str] = None,
    rerank_threshold: Optional[float] = None,
    rerank_threshold_mode: Optional[str] = None,
) -> RankingOptions:
    """Resolve canonical options while preserving the legacy REST switches.

    Precedence without an explicit profile:
    disabled rerank/backend=none -> fast; explicit fusion -> quality/semantic;
    explicit rerank=true enables the server's non-fast default (or quality when
    the default is fast); otherwise the server default is used.
    """

    default = parse_ranking_profile(default_profile)
    backend_none = (rerank_backend or "").strip().lower() == "none"
    warnings: Tuple[str, ...] = ()

    if ranking_profile is not None:
        profile = parse_ranking_profile(ranking_profile)
        if rerank_enabled is True and profile == "fast":
            raise ValueError("ranking_profile=fast 与 rerank_enabled=true 冲突")
        if rerank_enabled is False and profile != "fast":
            raise ValueError(
                f"ranking_profile={profile} 与 rerank_enabled=false 冲突"
            )
        if backend_none and profile != "fast":
            raise ValueError(
                f"ranking_profile={profile} 与 rerank_backend=none 冲突"
            )
        if fusion_enabled is not None:
            if profile == "fast":
                warnings = _with_warning(warnings, "FUSION_IGNORED_FAST_PROFILE")
            else:
                legacy_profile = "quality" if fusion_enabled else "semantic"
                if profile != legacy_profile:
                    raise ValueError(
                        f"ranking_profile={profile} 与 fusion_enabled={fusion_enabled} 冲突"
                    )
    else:
        if rerank_enabled is True and backend_none:
            raise ValueError("rerank_enabled=true 与 rerank_backend=none 冲突")
        if rerank_enabled is False or backend_none:
            profile = "fast"
            if fusion_enabled is not None:
                warnings = _with_warning(warnings, "FUSION_IGNORED_FAST_PROFILE")
        elif fusion_enabled is not None:
            profile = "quality" if fusion_enabled else "semantic"
        elif rerank_enabled is True and default == "fast":
            profile = "quality"
        else:
            profile = default

    threshold = default_threshold if rerank_threshold is None else rerank_threshold
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("rerank_threshold 必须是 0 到 1 之间的数字") from exc
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("rerank_threshold 必须在 0 到 1 之间")

    mode = parse_threshold_mode(
        default_threshold_mode
        if rerank_threshold_mode is None
        else rerank_threshold_mode
    )
    if threshold <= 0:
        mode = "off"
    if profile == "fast" and mode != "off":
        mode = "off"
        warnings = _with_warning(warnings, "THRESHOLD_SKIPPED_NO_SCORER")

    return RankingOptions(
        profile=profile,
        threshold=threshold,
        threshold_mode=mode,
        warnings=warnings,
    )
