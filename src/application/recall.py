"""多来源召回协调与 provider 级缓存策略。"""
from __future__ import annotations

from concurrent.futures import Executor, as_completed
from typing import Optional, Protocol, Sequence

from src.application.failures import search_failure
from src.application.outcomes import PlannedQuery, RecallOutcome
from src.cache import CacheBackend
from src.models import AcademicResult, PatentResult, SearchResult
from src.providers.base import SearchProvider


class RecallSettings(Protocol):
    cache_enabled: bool
    cache_ttl: int
    per_provider_k: int


class RecallCoordinator:
    """按查询计划并发召回 Web、学术和专利候选。"""

    def __init__(
        self,
        settings: RecallSettings,
        providers: Sequence[SearchProvider],
        academic_provider: Optional[SearchProvider],
        patent_provider: Optional[SearchProvider],
        cache: Optional[CacheBackend],
        executor: Executor,
    ) -> None:
        self._settings = settings
        self._providers = tuple(providers)
        self._academic_provider = academic_provider
        self._patent_provider = patent_provider
        self._cache = cache
        self._executor = executor

    def _cached_search(
        self,
        provider: SearchProvider,
        query: str,
        k: int,
        recency: Optional[str],
        use_cache: bool,
    ) -> list[SearchResult]:
        if not use_cache or self._cache is None:
            return provider.search(query, k, recency)
        key = f"{provider.name}|{k}|{recency or ''}|{query}"
        cached = self._cache.get(key)
        if cached is not None:
            return [item.model_copy(deep=True) for item in cached]
        items = provider.search(query, k, recency)
        self._cache.set(
            key,
            [item.model_copy(deep=True) for item in items],
            self._settings.cache_ttl,
        )
        return items

    def recall(self, planned: PlannedQuery) -> RecallOutcome:
        active_names = set(planned.active_provider_names)
        active = [provider for provider in self._providers if provider.name in active_names]
        tasks: list[tuple[str, SearchProvider, str]] = [
            ("web", provider, planned.search_query) for provider in active
        ]
        if planned.do_academic and self._academic_provider is not None:
            tasks.append(("academic", self._academic_provider, planned.academic_query))
        if planned.do_patent and self._patent_provider is not None:
            tasks.append(("patent", self._patent_provider, planned.search_query))

        web: list[SearchResult] = []
        academic: list[AcademicResult] = []
        patent: list[PatentResult] = []
        providers_used: list[str] = []
        failures = []
        use_cache = (
            self._settings.cache_enabled
            and self._cache is not None
            and not planned.plan.time_sensitive
        )

        futures = {
            self._executor.submit(
                self._cached_search,
                provider,
                query,
                self._settings.per_provider_k,
                planned.plan.recency,
                use_cache,
            ): (kind, provider.name)
            for kind, provider, query in tasks
        }
        for future in as_completed(futures):
            kind, name = futures[future]
            try:
                items = future.result()
                if kind == "academic":
                    academic.extend(items)  # type: ignore[arg-type]
                    if items:
                        providers_used.append(name)
                elif kind == "patent":
                    patent.extend(items)  # type: ignore[arg-type]
                    if items:
                        providers_used.append(name)
                else:
                    for provider_rank, item in enumerate(items):
                        item.provider_rank = provider_rank
                    web.extend(items)
                    # 保持旧契约：Web provider 即使返回空结果也记为已调用。
                    providers_used.append(name)
            except Exception as exc:
                failures.append(search_failure(
                    stage="provider_search",
                    source=name,
                    source_type=kind,
                    code="PROVIDER_SEARCH_FAILED",
                    message=exc,
                ))

        return RecallOutcome(
            web=tuple(web),
            academic=tuple(academic),
            patent=tuple(patent),
            providers_used=tuple(providers_used),
            planned_sources=tuple(provider.name for _, provider, _ in tasks),
            candidate_budget=self._settings.per_provider_k * len(tasks),
            failures=tuple(failures),
        )
