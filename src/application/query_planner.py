"""查询规划应用服务：封装 L0 理解、领域路由与学术查询改写。"""
from __future__ import annotations

from typing import Callable, Protocol, Sequence

from src.application.commands import SearchCommand
from src.application.failures import search_failure
from src.application.outcomes import PlannedQuery
from src.application.ports.query_rewriter import QueryRewriter
from src.l0 import plan_query
from src.domain.failures import SearchFailure
from src.domain.search import SearchPlan


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
class QueryPlanner:
    """把轻量 SearchCommand 转换为召回阶段可直接执行的查询计划。"""

    def __init__(
        self,
        settings: QueryPlannerSettings,
        rewriter: QueryRewriter | None = None,
        *,
        plan_query_fn: PlanQuery = plan_query,
    ) -> None:
        self._settings = settings
        self._rewriter = rewriter
        self._plan_query = plan_query_fn

    def plan(
        self,
        command: SearchCommand,
        provider_names: Sequence[str],
        *,
        academic_available: bool,
        patent_available: bool,
    ) -> PlannedQuery:
        """规划 Web/Academic/Patent 查询，并保留原链路的失败语义。"""
        top_k = command.limit
        rewrite = self._settings.rewrite_enabled
        requested = set(command.source_types or ())
        auto_route = command.source_types is None
        names = tuple(provider_names) if auto_route or "web" in requested else ()
        force_academic = None if auto_route else "academic" in requested
        force_patent = None if auto_route else "patent" in requested
        plan = self._plan_query(
            command.query,
            list(names),
            top_k,
            rewrite=False,
            academic_detect=self._settings.openalex_academic_detect,
            force_academic=force_academic,
            patent_detect=self._settings.patent_detect,
            force_patent=force_patent,
        )

        failures: list[SearchFailure] = list(plan.failures)
        if rewrite and self._settings.siliconflow_api_key and self._rewriter is not None:
            try:
                rewritten = self._rewriter.rewrite(plan.normalized_query)
            except Exception as exc:
                failures.append(search_failure(
                    stage="query_rewrite",
                    source="siliconflow",
                    code="QUERY_REWRITE_FAILED",
                    message=exc,
                ))
                rewritten = plan.normalized_query
            plan = plan.model_copy(update={
                "rewritten_query": rewritten,
                "failures": failures,
            })

        active_names = tuple(name for name in names if name in plan.providers)
        do_academic = bool(academic_available and plan.academic)
        do_patent = bool(patent_available and plan.patent)
        failures = list(plan.failures)

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
            if self._rewriter is not None:
                try:
                    academic_query = self._rewriter.rewrite(
                        search_query, academic=True
                    )
                except Exception as exc:
                    failures.append(search_failure(
                        stage="academic_query_rewrite",
                        source="siliconflow",
                        source_type="academic",
                        code="ACADEMIC_QUERY_REWRITE_FAILED",
                        message=exc,
                    ))

        return PlannedQuery(
            plan=plan,
            search_query=search_query,
            academic_query=academic_query,
            active_provider_names=active_names,
            do_academic=do_academic,
            do_patent=do_patent,
            failures=tuple(failures),
        )
