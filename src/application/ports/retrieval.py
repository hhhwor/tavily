"""来源能力、检索请求与批次结果的应用层契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from src.domain.documents import DocumentKind, FrozenMap, RetrievedDocument


RetrievalCapability = Literal[
    "recency_filter",
    "time_range_filter",
    "language_filter",
    "jurisdiction_filter",
    "full_content",
    "snippet",
    "open_access_metadata",
]
SnapshotCapability = Literal["provider_managed", "service_index", "index_alias"]


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """一个检索源可在注册期声明的稳定能力。"""

    id: str
    kind: DocumentKind
    capabilities: frozenset[RetrievalCapability] = frozenset()
    snapshot_capability: SnapshotCapability = "provider_managed"
    default_snapshot: str = "provider-managed"
    data_license: str = "provider-terms"
    default_language: str | None = None
    jurisdictions: tuple[str, ...] = ()
    max_candidates: int | None = None
    count_empty_as_used: bool = False

    def __post_init__(self) -> None:
        if not self.id or self.id.strip() != self.id:
            raise ValueError("SourceDescriptor.id 必须是非空且已清理的稳定标识")
        if self.max_candidates is not None and self.max_candidates <= 0:
            raise ValueError("SourceDescriptor.max_candidates 必须大于 0")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "jurisdictions", tuple(self.jurisdictions))


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """来源无关的检索边界；适配器必须报告实际应用的子集。"""

    query: str
    candidate_budget: int
    recency: str | None = None
    time_from: datetime | None = None
    time_to: datetime | None = None
    language: str | None = None
    jurisdiction: str | None = None

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("RetrievalRequest.query 不能为空")
        if self.candidate_budget <= 0:
            raise ValueError("RetrievalRequest.candidate_budget 必须大于 0")
        if self.time_from and self.time_to and self.time_from > self.time_to:
            raise ValueError("RetrievalRequest.time_from 不能晚于 time_to")


@dataclass(frozen=True, slots=True)
class RetrievalBatch:
    """一次来源调用的真实边界、不可变候选和诊断。"""

    source: SourceDescriptor
    documents: tuple[RetrievedDocument, ...] = ()
    actual_query: str = ""
    actual_filters: FrozenMap = field(default_factory=FrozenMap)
    snapshot: str = "snapshot-unavailable"
    limits: FrozenMap = field(default_factory=FrozenMap)
    elapsed_ms: int = 0
    diagnostics: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        object.__setattr__(self, "documents", tuple(self.documents))
        if not isinstance(self.actual_filters, FrozenMap):
            object.__setattr__(
                self, "actual_filters", FrozenMap.from_mapping(self.actual_filters)
            )
        if not isinstance(self.limits, FrozenMap):
            object.__setattr__(self, "limits", FrozenMap.from_mapping(self.limits))
        if not isinstance(self.diagnostics, FrozenMap):
            object.__setattr__(
                self, "diagnostics", FrozenMap.from_mapping(self.diagnostics)
            )


class RetrievalSource(Protocol):
    descriptor: SourceDescriptor

    def retrieve(self, request: RetrievalRequest) -> RetrievalBatch: ...
