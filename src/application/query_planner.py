"""查询规划应用服务：封装 L0 理解、领域路由与学术查询改写。"""
from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence

from src.application.commands import SearchCommand
from src.application.failures import search_failure
from src.application.outcomes import PlannedQuery
from src.l0 import plan_query, rewrite_academic_query
from src.models import SearchFailure, SearchPlan


class QueryPlannerSettings(Protocol):
    """QueryPlanner 实际消费的最小配置切片。"""

    default_top_k: int
    rewrite_enabled: bool
    siliconflow_api_key: str
    siliconflow_base_url: str
    rewrite_model: str
    rewrite_cache_size: int
    openalex_academic_detect: bool
    patent_detect: bool
    openalex_query_rewrite: bool


PlanQuery = Callable[..., SearchPlan]
AcademicQueryRewriter = Callable[..., str]


class QueryPlanner:
    """把兼容 SearchCommand 转换为召回阶段可直接执行的查询计划。"""

    def __init__(
        self,
        settings: QueryPlannerSettings,
        http_session: Any = None,
        *,
        plan_query_fn: PlanQuery = plan_query,
        academic_rewrite_fn: AcademicQueryRewriter = rewrite_academic_query,
    ) -> None:
        self._settings = settings
        self._http = http_session
        self._plan_query = plan_query_fn
        self._rewrite_academic_query = academic_rewrite_fn

    def plan(
        self,
        command: SearchCommand,
        provider_names: Sequence[str],
        *,
        academic_available: bool,
        patent_available: bool,
    ) -> PlannedQuery:
        """规划 Web/Academic/Patent 查询，并保留原链路的失败语义。"""
        top_k = command.top_k or self._settings.default_top_k
        rewrite = (
            self._settings.rewrite_enabled
            if command.rewrite_enabled is None
            else command.rewrite_enabled
        )
        names = tuple(provider_names)
        plan = self._plan_query(
            command.query,
            list(names),
            top_k,
            rewrite=rewrite,
            rewrite_api_key=self._settings.siliconflow_api_key,
            rewrite_base_url=self._settings.siliconflow_base_url,
            rewrite_model=self._settings.rewrite_model,
            rewrite_cache_size=self._settings.rewrite_cache_size,
            academic_detect=self._settings.openalex_academic_detect,
            force_academic=command.include_academic,
            patent_detect=self._settings.patent_detect,
            force_patent=command.include_patent,
            http_session=self._http,
        )

        active_names = tuple(name for name in names if name in plan.providers)
        do_academic = bool(academic_available and plan.academic)
        do_patent = bool(patent_available and plan.patent)
        failures: list[SearchFailure] = list(plan.failures)

        if plan.academic and not academic_available:
            failures.append(search_failure(
                stage="routing",
                source="openalex_local",
                source_type="academic",
                code="PROVIDER_UNAVAILABLE",
                message="学术检索被请求或自动触发,但 OpenAlex provider 未启用。",
            ))
        if plan.patent and not patent_available:
            failures.append(search_failure(
                stage="routing",
                source="patent_es",
                source_type="patent",
                code="PROVIDER_UNAVAILABLE",
                message="专利检索被请求或自动触发,但 Patent ES provider 未启用。",
            ))

        search_query = plan.rewritten_query or plan.normalized_query
        academic_query = search_query
        if (
            do_academic
            and self._settings.openalex_query_rewrite
            and self._settings.siliconflow_api_key
        ):
            academic_query = self._rewrite_academic_query(
                search_query,
                self._settings.siliconflow_api_key,
                self._settings.siliconflow_base_url,
                self._settings.rewrite_model,
                self._settings.rewrite_cache_size,
                failures=failures,
                http_session=self._http,
            )

        return PlannedQuery(
            plan=plan,
            search_query=search_query,
            academic_query=academic_query,
            active_provider_names=active_names,
            do_academic=do_academic,
            do_patent=do_patent,
            failures=tuple(failures),
        )
