"""SearchProvider 抽象基类 —— 所有搜索源实现此接口,输出统一的 SearchResult。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from time import perf_counter
from typing import Any, List, Mapping, Optional

from src.application.ports.retrieval import (
    RetrievalBatch,
    RetrievalRequest,
    SourceDescriptor,
)
from src.domain.documents import FrozenMap, RetrievedDocument
from src.domain.search import SearchResult


class SearchProvider(ABC):
    name: str = "base"
    descriptor = SourceDescriptor(id="base", kind="web")

    @abstractmethod
    def search(
        self, query: str, top_k: int = 10, recency: Optional[str] = None
    ) -> List[SearchResult]:
        """执行搜索,返回归一化结果列表。

        recency: None / day / week / month / year —— 由 L0 判定,实现需映射到
        各来源自己的时效过滤参数。实现还需自行处理来源特有的限制。
        """
        raise NotImplementedError

    def actual_query(self, request: RetrievalRequest) -> str:
        return request.query

    def actual_filters(self, request: RetrievalRequest) -> Mapping[str, Any]:
        return {"recency": request.recency} if request.recency else {}

    def snapshot(self, request: RetrievalRequest) -> str:
        return self.descriptor.default_snapshot

    def search_request(self, request: RetrievalRequest) -> List[SearchResult]:
        """兼容 hook；需要精确时间边界的适配器可覆盖。"""
        return self.search(
            request.query,
            request.candidate_budget,
            request.recency,
        )

    def limitations(self, request: RetrievalRequest) -> tuple[str, ...]:
        limitations: list[str] = []
        capabilities = self.descriptor.capabilities
        if request.language and "language_filter" not in capabilities:
            limitations.append("LANGUAGE_FILTER_UNSUPPORTED")
        if request.jurisdiction and "jurisdiction_filter" not in capabilities:
            limitations.append("JURISDICTION_FILTER_UNSUPPORTED")
        if request.time_from and not ({"time_range_filter", "recency_filter"} & capabilities):
            limitations.append("TIME_FILTER_UNSUPPORTED")
        return tuple(limitations)

    def retrieve(self, request: RetrievalRequest) -> RetrievalBatch:
        """把旧 ``search`` 适配为带真实边界的不可变批次。"""
        started = perf_counter()
        actual_query = self.actual_query(request)
        actual_filters = dict(self.actual_filters(request))
        snapshot = self.snapshot(request)
        results = self.search_request(request)
        documents = tuple(
            item
            if isinstance(item, RetrievedDocument)
            else RetrievedDocument.from_result(
                item,
                self.descriptor.kind,
                provider_rank=rank,
                snapshot=snapshot,
                actual_filters=actual_filters,
                provider=self.descriptor.id,
            )
            for rank, item in enumerate(results)
        )
        return RetrievalBatch(
            source=self.descriptor,
            documents=documents,
            actual_query=actual_query,
            actual_filters=FrozenMap.from_mapping(actual_filters),
            snapshot=snapshot,
            limits=FrozenMap.from_mapping({
                "requested_candidates": request.candidate_budget,
                "provider_max_candidates": self.descriptor.max_candidates,
                "returned_candidates": len(documents),
            }),
            elapsed_ms=int((perf_counter() - started) * 1000),
            diagnostics=FrozenMap.from_mapping({
                "limitations": self.limitations(request),
                "data_license": self.descriptor.data_license,
            }),
        )
