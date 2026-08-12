"""多来源召回协调与 provider 级缓存策略。"""
from __future__ import annotations

from concurrent.futures import Executor, TimeoutError, as_completed
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

from src.application.failures import search_failure
from src.application.commands import SearchFilters
from src.application.outcomes import PlannedQuery, RecallOutcome
from src.application.ports.cache import CacheBackend
from src.application.ports.resilience import ResiliencePolicy
from src.application.ports.runtime import Deadline, DeadlineExceededError
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
    provider_timeout: int


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
        resilience: ResiliencePolicy | None = None,
        research_executor: Executor | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._cache = cache
        self._executor = executor
        self._research_executor = research_executor or executor
        self._clock = clock
        self._resilience = resilience

    def _cached_retrieve(
        self,
        source: RetrievalSource,
        request: RetrievalRequest,
        use_cache: bool,
        deadline: Deadline | None,
    ) -> RetrievalBatch:
        def retrieve() -> RetrievalBatch:
            bounded = self._bound_request_timeout(request, deadline)
            return source.retrieve(bounded)

        def resilient_retrieve() -> RetrievalBatch:
            if self._resilience is None:
                return retrieve()
            return self._resilience.call(
                source.descriptor.id,
                "search",
                retrieve,
                deadline=deadline,
            )

        if not use_cache or self._cache is None:
            return resilient_retrieve()
        key = "|".join((
            source.descriptor.id,
            str(request.candidate_budget),
            request.recency or "",
            ",".join(request.languages),
            ",".join(request.jurisdictions),
            request.time_from.isoformat() if request.time_from else "",
            request.time_to.isoformat() if request.time_to else "",
            request.legal_status or "",
            request.query,
        ))
        cached = self._cache.get(key)
        if cached is not None:
            if not isinstance(cached, RetrievalBatch):
                raise TypeError("retrieval cache value must be RetrievalBatch")
            return cached
        batch = resilient_retrieve()
        self._cache.set(key, batch, self._settings.cache_ttl)
        return batch

    def _bound_request_timeout(
        self,
        request: RetrievalRequest,
        deadline: Deadline | None,
    ) -> RetrievalRequest:
        timeout_seconds = float(self._settings.provider_timeout)
        if deadline is not None:
            remaining = deadline.remaining_seconds()
            if remaining <= 0:
                raise DeadlineExceededError("search deadline exceeded")
            timeout_seconds = min(timeout_seconds, remaining)
        return replace(request, timeout_seconds=max(0.001, timeout_seconds))

    @staticmethod
    def _language(query: str) -> str | None:
        if any("\u4e00" <= char <= "\u9fff" for char in query):
            return "zh"
        if any(char.isascii() and char.isalpha() for char in query):
            return "en"
        return None

    def _request(
        self,
        query: str,
        recency: str | None,
        filters: SearchFilters,
    ) -> RetrievalRequest:
        now = self._clock()
        delta = {
            "day": timedelta(days=1),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
            "year": timedelta(days=365),
        }.get(recency or "")
        time_from = (
            datetime.combine(filters.published_from, datetime.min.time(), tzinfo=now.tzinfo)
            if filters.published_from
            else (now - delta if delta else None)
        )
        time_to = (
            datetime.combine(filters.published_to, datetime.max.time(), tzinfo=now.tzinfo)
            if filters.published_to
            else (now if delta else None)
        )
        languages = filters.languages or tuple(filter(None, (self._language(query),)))
        return RetrievalRequest(
            query=query,
            candidate_budget=self._settings.per_provider_k,
            recency=recency,
            time_from=time_from,
            time_to=time_to,
            languages=languages,
            jurisdictions=filters.jurisdictions,
            legal_status=filters.legal_status,
        )

    def recall(
        self,
        planned: PlannedQuery,
        *,
        filters: SearchFilters | None = None,
        deadline: Deadline | None = None,
        workload_class: str = "search",
        allow_shared_cache: bool = True,
        candidate_budget: int | None = None,
    ) -> RecallOutcome:
        filters = filters or SearchFilters()
        active_names = set(planned.active_provider_names)
        tasks: list[tuple[RetrievalSource, RetrievalRequest]] = []
        for source in self._registry.sources("web"):
            if source.descriptor.id in active_names:
                tasks.append((
                    source,
                    self._request(planned.search_query, planned.plan.recency, filters),
                ))
        if planned.do_academic:
            tasks.extend(
                (source, self._request(planned.academic_query, planned.plan.recency, filters))
                for source in self._registry.sources("academic")
            )
        if planned.do_patent:
            tasks.extend(
                (source, self._request(planned.search_query, planned.plan.recency, filters))
                for source in self._registry.sources("patent")
            )
        if planned.do_legal:
            tasks.extend(
                (source, self._request(planned.search_query, planned.plan.recency, filters))
                for source in self._registry.sources("legal")
            )
        if candidate_budget is not None:
            remaining_budget = max(0, candidate_budget)
            bounded_tasks: list[tuple[RetrievalSource, RetrievalRequest]] = []
            for index, (source, request) in enumerate(tasks):
                sources_left = len(tasks) - index
                if remaining_budget <= 0:
                    break
                share = max(1, remaining_budget // max(1, sources_left))
                allocation = min(request.candidate_budget, share)
                bounded_tasks.append((
                    source,
                    replace(request, candidate_budget=allocation),
                ))
                remaining_budget -= allocation
            tasks = bounded_tasks

        web: list[RetrievedDocument] = []
        academic: list[RetrievedDocument] = []
        patent: list[RetrievedDocument] = []
        legal: list[RetrievedDocument] = []
        batches: list[RetrievalBatch] = []
        providers_used: list[str] = []
        failures = []
        use_cache = (
            allow_shared_cache
            and self._settings.cache_enabled
            and self._cache is not None
            and not planned.plan.time_sensitive
        )

        executor = (
            self._research_executor
            if workload_class == "research"
            else self._executor
        )
        futures = {
            executor.submit(
                self._cached_retrieve,
                source,
                request,
                use_cache,
                deadline,
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
                elif descriptor.kind == "legal":
                    legal.extend(items)
                else:
                    web.extend(items)
                if items or descriptor.count_empty_as_used:
                    providers_used.append(descriptor.id)
            except DeadlineExceededError:
                failures.append(search_failure(
                    stage="provider_search",
                    source=descriptor.id,
                    source_type=descriptor.kind,
                    code="SEARCH_DEADLINE_EXCEEDED",
                    message="search deadline exceeded",
                ))
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
            legal=tuple(legal),
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
