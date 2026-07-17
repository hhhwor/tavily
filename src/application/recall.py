"""多来源召回协调与 provider 级缓存策略。"""
from __future__ import annotations

from concurrent.futures import Executor, TimeoutError, as_completed
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

from src.application.failures import search_failure
from src.application.outcomes import PlannedQuery, RecallOutcome
from src.application.ports.cache import CacheBackend
from src.application.ports.runtime import Deadline
from src.application.ports.retrieval import (
    RetrievalBatch,
    RetrievalRequest,
    RetrievalSource,
)
from src.application.source_registry import SourceRegistry
from src.domain.documents import RetrievedDocument


class RecallSettings(Protocol):
    cache_enabled: bool
    cache_ttl: int
    per_provider_k: int


class RecallCoordinator:
    """按查询计划并发召回 Web、学术和专利候选。"""

    def __init__(
        self,
        settings: RecallSettings,
        registry: SourceRegistry,
        cache: Optional[CacheBackend],
        executor: Executor,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._cache = cache
        self._executor = executor
        self._clock = clock

    def _cached_retrieve(
        self,
        source: RetrievalSource,
        request: RetrievalRequest,
        use_cache: bool,
    ) -> RetrievalBatch:
        if not use_cache or self._cache is None:
            return source.retrieve(request)
        key = "|".join((
            source.descriptor.id,
            str(request.candidate_budget),
            request.recency or "",
            request.language or "",
            request.jurisdiction or "",
            request.query,
        ))
        cached = self._cache.get(key)
        if cached is not None:
            if not isinstance(cached, RetrievalBatch):
                raise TypeError("retrieval cache value must be RetrievalBatch")
            return cached
        batch = source.retrieve(request)
        self._cache.set(key, batch, self._settings.cache_ttl)
        return batch

    @staticmethod
    def _language(query: str) -> str | None:
        if any("\u4e00" <= char <= "\u9fff" for char in query):
            return "zh"
        if any(char.isascii() and char.isalpha() for char in query):
            return "en"
        return None

    def _request(self, query: str, recency: str | None) -> RetrievalRequest:
        now = self._clock()
        delta = {
            "day": timedelta(days=1),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
            "year": timedelta(days=365),
        }.get(recency or "")
        return RetrievalRequest(
            query=query,
            candidate_budget=self._settings.per_provider_k,
            recency=recency,
            time_from=now - delta if delta else None,
            time_to=now if delta else None,
            language=self._language(query),
            jurisdiction=None,
        )

    def recall(
        self,
        planned: PlannedQuery,
        *,
        deadline: Deadline | None = None,
    ) -> RecallOutcome:
        active_names = set(planned.active_provider_names)
        tasks: list[tuple[RetrievalSource, RetrievalRequest]] = []
        for source in self._registry.sources("web"):
            if source.descriptor.id in active_names:
                tasks.append((
                    source,
                    self._request(planned.search_query, planned.plan.recency),
                ))
        if planned.do_academic:
            tasks.extend(
                (source, self._request(planned.academic_query, planned.plan.recency))
                for source in self._registry.sources("academic")
            )
        if planned.do_patent:
            tasks.extend(
                (source, self._request(planned.search_query, planned.plan.recency))
                for source in self._registry.sources("patent")
            )

        web: list[RetrievedDocument] = []
        academic: list[RetrievedDocument] = []
        patent: list[RetrievedDocument] = []
        batches: list[RetrievalBatch] = []
        providers_used: list[str] = []
        failures = []
        use_cache = (
            self._settings.cache_enabled
            and self._cache is not None
            and not planned.plan.time_sensitive
        )

        futures = {
            self._executor.submit(
                self._cached_retrieve,
                source,
                request,
                use_cache,
            ): source.descriptor
            for source, request in tasks
        }
        processed = set()

        def collect(future) -> None:
            descriptor = futures[future]
            processed.add(future)
            try:
                batch = future.result()
                batches.append(batch)
                items = batch.documents
                if descriptor.kind == "academic":
                    academic.extend(items)
                elif descriptor.kind == "patent":
                    patent.extend(items)
                else:
                    web.extend(items)
                if items or descriptor.count_empty_as_used:
                    providers_used.append(descriptor.id)
            except Exception as exc:
                failures.append(search_failure(
                    stage="provider_search",
                    source=descriptor.id,
                    source_type=descriptor.kind,
                    code="PROVIDER_SEARCH_FAILED",
                    message=exc,
                ))

        try:
            timeout = deadline.remaining_seconds() if deadline is not None else None
            for future in as_completed(futures, timeout=timeout):
                collect(future)
        except TimeoutError:
            pass

        for future, descriptor in futures.items():
            if future in processed:
                continue
            if future.done():
                collect(future)
                continue
            future.cancel()
            failures.append(search_failure(
                stage="provider_search",
                source=descriptor.id,
                source_type=descriptor.kind,
                code="SEARCH_DEADLINE_EXCEEDED",
                message="search deadline exceeded",
            ))

        return RecallOutcome(
            web=tuple(web),
            academic=tuple(academic),
            patent=tuple(patent),
            batches=tuple(batches),
            providers_used=tuple(providers_used),
            planned_sources=tuple(
                source.descriptor.id for source, _ in tasks
            ),
            candidate_budget=sum(
                request.candidate_budget for _, request in tasks
            ),
            failures=tuple(failures),
        )
