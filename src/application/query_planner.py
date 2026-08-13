"""查询规划应用服务：封装 L0 理解、领域路由与学术查询改写。"""
from __future__ import annotations

from typing import Callable, Protocol, Sequence

from src.application.commands import SearchCommand
from src.application.failures import search_failure
from src.application.outcomes import PlannedQuery
from src.application.ports.intent_classifier import IntentClassifier
from src.application.ports.query_rewriter import QueryRewriter
from src.application.ports.resilience import ResiliencePolicy
from src.application.ports.runtime import Deadline, DeadlineExceededError
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
    intent_classifier_enabled: bool
    intent_classifier_min_confidence: float
    openalex_academic_detect: bool
    patent_detect: bool
    fy_law_mcp_detect: bool
    openalex_query_rewrite: bool


PlanQuery = Callable[..., SearchPlan]
class QueryPlanner:
    """把轻量 SearchCommand 转换为召回阶段可直接执行的查询计划。"""

    def __init__(
        self,
        settings: QueryPlannerSettings,
        rewriter: QueryRewriter | None = None,
        *,
        intent_classifier: IntentClassifier | None = None,
        plan_query_fn: PlanQuery = plan_query,
        resilience: ResiliencePolicy | None = None,
    ) -> None:
        self._settings = settings
        self._rewriter = rewriter
        self._intent_classifier = intent_classifier
        self._plan_query = plan_query_fn
        self._resilience = resilience

    def plan(
        self,
        command: SearchCommand,
        provider_names: Sequence[str],
        *,
        academic_available: bool,
        patent_available: bool,
        legal_available: bool = False,
        deadline: Deadline | None = None,
        allow_external_models: bool = True,
    ) -> PlannedQuery:
        """规划 Web/Academic/Patent 查询，并保留原链路的失败语义。"""
        top_k = command.limit
        rewrite = self._settings.rewrite_enabled
        requested = set(command.source_types or ())
        auto_route = command.source_types is None
        names = tuple(provider_names) if auto_route or "web" in requested else ()
        force_academic = None if auto_route else "academic" in requested
        force_patent = None if auto_route else "patent" in requested
        force_legal = None if auto_route else "legal" in requested
        plan = self._plan_query(
            command.query,
            list(names),
            top_k,
            rewrite=False,
            academic_detect=self._settings.openalex_academic_detect,
            force_academic=force_academic,
            patent_detect=self._settings.patent_detect,
            force_patent=force_patent,
            legal_detect=self._settings.fy_law_mcp_detect,
            force_legal=force_legal,
        )

        failures: list[SearchFailure] = list(plan.failures)
        if (
            auto_route
            and allow_external_models
            and self._settings.intent_classifier_enabled
            and self._settings.siliconflow_api_key
            and self._intent_classifier is not None
        ):
            try:
                decision = self._classify(
                    plan.normalized_query,
                    deadline=deadline,
                )
                if (
                    decision.confidence
                    >= self._settings.intent_classifier_min_confidence
                ):
                    model_sources = set(decision.source_types)
                    rule_sources = {
                        source
                        for source, enabled in (
                            ("academic", plan.academic),
                            ("patent", plan.patent),
                            ("legal", plan.legal),
                        )
                        if enabled
                    }
                    authoritative = bool(getattr(
                        self._intent_classifier,
                        "authoritative_routes",
                        False,
                    ))
                    if authoritative:
                        # The embedding classifier includes general hard
                        # negatives and may suppress one noisy keyword rule.
                        # Two rule hits already establish an explicit mixed
                        # request and are safer than an extra correlated score.
                        effective_sources = (
                            rule_sources
                            if len(rule_sources) >= 2
                            else model_sources
                        )
                    else:
                        # The legacy chat classifier only expands rule routes.
                        if len(rule_sources) >= 2:
                            model_sources &= rule_sources
                        effective_sources = rule_sources | model_sources
                    plan = plan.model_copy(update={
                        "academic": "academic" in effective_sources,
                        "patent": "patent" in effective_sources,
                        "legal": "legal" in effective_sources,
                        "intent": self._effective_intent(effective_sources),
                        "intent_confidence": (
                            decision.confidence
                            if effective_sources == model_sources
                            else None
                        ),
                        "intent_source_scores": decision.source_scores,
                        "legal_mode": (
                            decision.legal_mode
                            if "legal" in effective_sources
                            else None
                        ),
                    })
            except DeadlineExceededError:
                failures.append(search_failure(
                    stage="intent_classification",
                    source="siliconflow",
                    code="SEARCH_DEADLINE_EXCEEDED",
                    message="search deadline exceeded",
                ))
                plan = plan.model_copy(update={"failures": failures})
            except Exception as exc:
                failures.append(search_failure(
                    stage="intent_classification",
                    source="siliconflow",
                    code="INTENT_CLASSIFICATION_FAILED",
                    message=exc,
                ))
                plan = plan.model_copy(update={"failures": failures})
        if (
            allow_external_models
            and rewrite
            and self._settings.siliconflow_api_key
            and self._rewriter is not None
        ):
            try:
                rewritten = self._rewrite(
                    plan.normalized_query,
                    deadline=deadline,
                )
            except DeadlineExceededError:
                failures.append(search_failure(
                    stage="query_rewrite",
                    source="siliconflow",
                    code="SEARCH_DEADLINE_EXCEEDED",
                    message="search deadline exceeded",
                ))
                rewritten = plan.normalized_query
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
        do_legal = bool(legal_available and plan.legal)
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
        if plan.legal and not legal_available:
            failures.append(search_failure(
                stage="routing",
                source="fy_law_mcp",
                source_type="legal",
                code="PROVIDER_UNAVAILABLE",
                message="法律法规检索被请求或自动触发,但 FY provider 未启用。",
            ))

        search_query = plan.rewritten_query or plan.normalized_query
        academic_query = search_query
        if (
            allow_external_models
            and do_academic
            and self._settings.openalex_query_rewrite
            and self._settings.siliconflow_api_key
        ):
            if self._rewriter is not None:
                try:
                    academic_query = self._rewrite(
                        search_query,
                        academic=True,
                        deadline=deadline,
                    )
                except DeadlineExceededError:
                    failures.append(search_failure(
                        stage="academic_query_rewrite",
                        source="siliconflow",
                        source_type="academic",
                        code="SEARCH_DEADLINE_EXCEEDED",
                        message="search deadline exceeded",
                    ))
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
            do_legal=do_legal,
            failures=tuple(failures),
        )

    @staticmethod
    def _effective_intent(sources: set[str]) -> str:
        if len(sources) > 1:
            return "mixed_research"
        if not sources:
            return "general_search"
        return {
            "academic": "academic_literature",
            "patent": "patent",
            "legal": "legal",
        }[next(iter(sources))]

    def _rewrite(
        self,
        query: str,
        *,
        academic: bool = False,
        deadline: Deadline | None = None,
    ) -> str:
        def invoke() -> str:
            timeout_seconds = None
            if deadline is not None:
                timeout_seconds = deadline.remaining_seconds()
                if timeout_seconds <= 0:
                    raise DeadlineExceededError("search deadline exceeded")
            rewrite_with_timeout = getattr(
                self._rewriter, "rewrite_with_timeout", None
            )
            if callable(rewrite_with_timeout):
                return rewrite_with_timeout(
                    query,
                    academic=academic,
                    timeout_seconds=timeout_seconds,
                )
            return self._rewriter.rewrite(query, academic=academic)

        if self._resilience is None:
            return invoke()
        dependency = (
            "siliconflow:academic-rewrite"
            if academic
            else "siliconflow:query-rewrite"
        )
        return self._resilience.call(
            dependency,
            "rewrite",
            invoke,
            deadline=deadline,
        )

    def _classify(self, query: str, *, deadline: Deadline | None = None):
        def invoke():
            timeout_seconds = None
            if deadline is not None:
                timeout_seconds = deadline.remaining_seconds()
                if timeout_seconds <= 0:
                    raise DeadlineExceededError("search deadline exceeded")
            classify_with_timeout = getattr(
                self._intent_classifier, "classify_with_timeout", None
            )
            if callable(classify_with_timeout):
                return classify_with_timeout(query, timeout_seconds=timeout_seconds)
            return self._intent_classifier.classify(query)  # type: ignore[union-attr]

        if self._resilience is None:
            return invoke()
        return self._resilience.call(
            "siliconflow:intent-classification",
            "intent_classification",
            invoke,
            deadline=deadline,
        )
