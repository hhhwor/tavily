"""Application 层公开命令契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.domain.documents import DocumentKind
from src.domain.research import (
    ResearchBudget,
    ResearchObjective,
    ResearchPrivacy,
    ResearchScope,
)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """轻量检索允许调用方声明的稳定过滤边界。"""

    published_from: date | None = None
    published_to: date | None = None
    languages: tuple[str, ...] = ()
    jurisdictions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.published_from and self.published_to:
            if self.published_from > self.published_to:
                raise ValueError("published_from 不能晚于 published_to")
        object.__setattr__(self, "languages", tuple(self.languages))
        object.__setattr__(self, "jurisdictions", tuple(self.jurisdictions))


@dataclass(frozen=True, slots=True)
class SearchCommand:
    """轻量搜索的唯一应用层输入，不暴露底层模型与研究参数。"""

    query: str
    limit: int = 10
    source_types: tuple[DocumentKind, ...] | None = None
    filters: SearchFilters = field(default_factory=SearchFilters)

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query 不能为空")
        if not 1 <= self.limit <= 20:
            raise ValueError("limit 必须在 1 到 20 之间")
        if self.source_types is not None:
            normalized = tuple(dict.fromkeys(self.source_types))
            if not normalized:
                raise ValueError("source_types 不能是空数组")
            object.__setattr__(self, "source_types", normalized)


@dataclass(frozen=True, slots=True)
class ResearchCommand:
    search_id: str
    profile: str = "technology_validation"
    depth: str = "standard"
    objective: ResearchObjective | None = None
    scope: ResearchScope | None = None
    policy: str | None = None
    budget: ResearchBudget | None = None
    privacy: ResearchPrivacy | None = None


@dataclass(frozen=True, slots=True)
class ResearchFeedbackCommand:
    task_revision: int
    answers: dict[str, str] = field(default_factory=dict)
    note: str | None = None
