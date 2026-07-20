"""Shared query planning, recall and ranking pipeline."""
from __future__ import annotations

from src.application.commands import SearchCommand
from src.application.outcomes import DiscoveryOutcome
from src.application.ports.runtime import Clock, Deadline
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
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
    ) -> DiscoveryOutcome:
        active_deadline = deadline or Deadline.after(self._deadline_ms, self._clock)
        options = self._ranking.resolve()
        query_time = self._clock.now()
        planned = self._query_planner.plan(
            command,
            self._source_registry.ids("web"),
            academic_available=self._source_registry.has_kind("academic"),
            patent_available=self._source_registry.has_kind("patent"),
        )
        recalled = self._recall.recall(
            planned,
            filters=command.filters,
            deadline=active_deadline,
        )
        ranked = self._ranking.rank(
            command,
            planned,
            recalled,
            options=options,
            deadline=active_deadline,
        )
        return DiscoveryOutcome(
            query_time=query_time,
            planned=planned,
            recalled=recalled,
            ranked=ranked,
        )
