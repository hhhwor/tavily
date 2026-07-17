"""按显式 descriptor 注册和选择检索来源。"""
from __future__ import annotations

from types import MappingProxyType
from typing import Iterable

from src.application.ports.retrieval import RetrievalSource, SourceDescriptor
from src.domain.documents import DocumentKind


class SourceRegistry:
    """组合根构建后只读的来源注册表。"""

    def __init__(self, sources: Iterable[RetrievalSource] = ()) -> None:
        indexed: dict[str, RetrievalSource] = {}
        for source in sources:
            descriptor = source.descriptor
            if descriptor.id in indexed:
                raise ValueError(f"重复的 source id: {descriptor.id}")
            indexed[descriptor.id] = source
        self._sources = MappingProxyType(indexed)

    def get(self, source_id: str) -> RetrievalSource | None:
        return self._sources.get(source_id)

    def sources(self, kind: DocumentKind | None = None) -> tuple[RetrievalSource, ...]:
        return tuple(
            source
            for source in self._sources.values()
            if kind is None or source.descriptor.kind == kind
        )

    def descriptors(self, kind: DocumentKind | None = None) -> tuple[SourceDescriptor, ...]:
        return tuple(source.descriptor for source in self.sources(kind))

    def ids(self, kind: DocumentKind | None = None) -> tuple[str, ...]:
        return tuple(descriptor.id for descriptor in self.descriptors(kind))

    def has_kind(self, kind: DocumentKind) -> bool:
        return any(True for _ in self.sources(kind))

    def snapshot_for(self, source_id: str) -> str:
        source = self.get(source_id)
        return (
            source.descriptor.default_snapshot
            if source is not None
            else "snapshot-unavailable"
        )
