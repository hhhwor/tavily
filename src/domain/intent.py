"""Immutable model-assisted query intent contract."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


IntentKind = Literal[
    "general_search",
    "legal",
    "academic_literature",
    "patent",
    "mixed_research",
]
IntentSourceType = Literal["academic", "patent", "legal"]
LegalMode = Literal["exact_citation", "interpretation", "general"]

_VALID_INTENTS = frozenset({
    "general_search",
    "legal",
    "academic_literature",
    "patent",
    "mixed_research",
})
_VALID_SOURCES = frozenset({"academic", "patent", "legal"})
_SOURCE_ORDER = ("academic", "patent", "legal")
_SINGLE_INTENT_SOURCES = {
    "general_search": (),
    "legal": ("legal",),
    "academic_literature": ("academic",),
    "patent": ("patent",),
}
_VALID_LEGAL_MODES = frozenset({
    "exact_citation", "interpretation", "general",
})


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """Validated output of a model intent classifier.

    ``legal`` is the single legal route label. ``legal_mode`` is optional
    metadata for later legal-query specialization; it is not a second route.
    """

    intent: IntentKind
    source_types: tuple[IntentSourceType, ...]
    confidence: float
    legal_mode: LegalMode | None = None
    source_scores: tuple[tuple[IntentSourceType, float], ...] = ()

    def __post_init__(self) -> None:
        if self.intent not in _VALID_INTENTS:
            raise ValueError("intent 不受支持")
        if isinstance(self.confidence, bool) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 必须在 0 到 1 之间")
        sources = tuple(dict.fromkeys(self.source_types))
        if any(source not in _VALID_SOURCES for source in sources):
            raise ValueError("source_types 不受支持")
        sources = tuple(source for source in _SOURCE_ORDER if source in sources)
        expected = _SINGLE_INTENT_SOURCES.get(self.intent)
        if expected is not None and sources != expected:
            raise ValueError("单一 intent 的 source_types 不匹配")
        if self.intent == "mixed_research" and len(sources) < 2:
            raise ValueError("mixed_research 至少需要两个垂类 source_types")
        if self.legal_mode is not None and self.legal_mode not in _VALID_LEGAL_MODES:
            raise ValueError("legal_mode 不受支持")
        if "legal" not in sources and self.legal_mode is not None:
            raise ValueError("非法律意图不能设置 legal_mode")
        scores: dict[IntentSourceType, float] = {}
        for source, score in self.source_scores:
            if source not in _VALID_SOURCES:
                raise ValueError("source_scores 包含不受支持的来源")
            if source in scores:
                raise ValueError("source_scores 不能包含重复来源")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or not 0.0 <= float(score) <= 1.0
            ):
                raise ValueError("source_scores 必须在 0 到 1 之间")
            scores[source] = float(score)
        object.__setattr__(self, "source_types", sources)
        object.__setattr__(
            self,
            "source_scores",
            tuple(
                (source, scores[source])
                for source in _SOURCE_ORDER
                if source in scores
            ),
        )
