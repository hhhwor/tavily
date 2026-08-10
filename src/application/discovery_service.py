"""Shared query planning, recall and ranking pipeline."""
from __future__ import annotations

from src.application.commands import SearchCommand
from src.application.outcomes import DiscoveryOutcome
from src.application.ports.runtime import Clock, Deadline
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.research_execution import CancellationToken
from src.application.source_registry import SourceRegistry


class DiscoveryService:
    """执行单轮发现，不组装公开响应，也不做事实可信度判断。"""

    def __init__(
        self,
        *,
        query_planner: QueryPlanner,
        recall: RecallCoordinator,
        ranking: RankingService,
        source_registry: SourceRegistry,
        clock: Clock,
        deadline_ms: int,
    ) -> None:
        self._query_planner = query_planner
        self._recall = recall
        self._ranking = ranking
        self._source_registry = source_registry
        self._clock = clock
        self._deadline_ms = deadline_ms

    def execute(
        self,
        command: SearchCommand,
        *,
        deadline: Deadline | None = None,
        allow_external_models: bool = True,
        workload_class: str = "search",
        allow_shared_cache: bool = True,
        candidate_budget: int | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DiscoveryOutcome:
        active_deadline = deadline or Deadline.after(self._deadline_ms, self._clock)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        options = self._ranking.resolve()
        query_time = self._clock.now()
        planned = self._query_planner.plan(
            command,
            self._source_registry.ids("web"),
            academic_available=self._source_registry.has_kind("academic"),
            patent_available=self._source_registry.has_kind("patent"),
            legal_available=self._source_registry.has_kind("legal"),
            deadline=active_deadline,
            allow_external_models=allow_external_models,
        )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        recalled = self._recall.recall(
            planned,
            filters=command.filters,
            deadline=active_deadline,
            workload_class=workload_class,
            allow_shared_cache=allow_shared_cache,
            candidate_budget=candidate_budget,
        )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        ranked = self._ranking.rank(
            command,
            planned,
            recalled,
            options=options,
            deadline=active_deadline,
            allow_external_models=allow_external_models,
            workload_class=workload_class,
        )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return DiscoveryOutcome(
            query_time=query_time,
            planned=planned,
            recalled=recalled,
            ranked=ranked,
        )
