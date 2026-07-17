"""Application 各阶段的不可变输出契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple, TypeVar

from src.models import (
    AcademicResult,
    PatentResult,
    SearchFailure,
    SearchPlan,
    SearchResult,
)
from src.pipeline.ranking_options import RankingOptions


T = TypeVar("T")


def _tuple(value: Sequence[T]) -> Tuple[T, ...]:
    """允许服务传入任意 Sequence，但在阶段边界统一冻结为 tuple。"""
    return value if isinstance(value, tuple) else tuple(value)


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    """查询理解、路由判定与各领域实际查询文本。"""

    plan: SearchPlan
    search_query: str
    academic_query: str
    active_provider_names: Tuple[str, ...] = ()
    do_academic: bool = False
    do_patent: bool = False
    failures: Tuple[SearchFailure, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "active_provider_names", _tuple(self.active_provider_names)
        )
        object.__setattr__(self, "failures", _tuple(self.failures))


@dataclass(frozen=True, slots=True)
class RecallOutcome:
    """多来源召回阶段结果。"""

    web: Tuple[SearchResult, ...] = ()
    academic: Tuple[AcademicResult, ...] = ()
    patent: Tuple[PatentResult, ...] = ()
    providers_used: Tuple[str, ...] = ()
    planned_sources: Tuple[str, ...] = ()
    candidate_budget: int = 0
    failures: Tuple[SearchFailure, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "web",
            "academic",
            "patent",
            "providers_used",
            "planned_sources",
            "failures",
        ):
            object.__setattr__(self, field_name, _tuple(getattr(self, field_name)))


@dataclass(frozen=True, slots=True)
class RankingOutcome:
    """领域重排阶段结果与实际生效的排序配置。"""

    options: RankingOptions
    reranker: str
    web: Tuple[SearchResult, ...] = ()
    academic: Tuple[AcademicResult, ...] = ()
    patent: Tuple[PatentResult, ...] = ()
    failures: Tuple[SearchFailure, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("web", "academic", "patent", "failures"):
            object.__setattr__(self, field_name, _tuple(getattr(self, field_name)))


@dataclass(frozen=True, slots=True)
class PdfEnrichmentOutcome:
    """PDF 正文富化后的学术候选及结构化失败。"""

    academic: Tuple[AcademicResult, ...] = ()
    failures: Tuple[SearchFailure, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "academic", _tuple(self.academic))
        object.__setattr__(self, "failures", _tuple(self.failures))
