"""排序 Profile 解析、scorer 生命周期与三领域重排协调。"""
from __future__ import annotations

from concurrent.futures import Executor, TimeoutError, as_completed
from threading import RLock
from typing import Callable, Protocol

from src.application.commands import SearchCommand
from src.application.failures import search_failure
from src.application.outcomes import PlannedQuery, RankingOutcome, RecallOutcome
from src.application.ports.runtime import Clock
from src.application.ports.runtime import Deadline
from src.domain.documents import (
    DocumentKind,
    RankedDocument,
    RetrievedDocument,
    SourceAttribution,
)
from src.models import AcademicResult, PatentResult, SearchResult
from src.pipeline.dedup import normalize_url
from src.pipeline.ranking_options import RankingOptions, resolve_ranking_options
from src.ranking import (
    AcademicReranker,
    PatentReranker,
    Reranker,
    WebReranker,
    build_rerank_context,
)


class RankingSettings(Protocol):
    ranking_profile: str
    rerank_backend: str
    rerank_model: str
    rerank_threshold: float
    rerank_threshold_mode: str
    rerank_enabled: bool


class RankingService:
    """选择文本 scorer，并对三个来源域进行可独立降级的重排。"""

    def __init__(
        self,
        settings: RankingSettings,
        text_scorer: Reranker,
        text_scorer_factory: Callable[[bool, str, str], Reranker],
        executor: Executor,
        *,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self.text_scorer = text_scorer
        self._factory = text_scorer_factory
        self._executor = executor
        self._clock = clock
        self._scorer_cache: dict[tuple[bool, str, str], Reranker] = {}
        self._scorer_lock = RLock()
        self._closed = False

    def _select_text_scorer(
        self,
        enabled: bool | None,
        backend: str | None,
        model: str | None,
    ) -> Reranker:
        if enabled is None and backend is None and model is None:
            return self.text_scorer
        key = (
            self._settings.rerank_enabled if enabled is None else enabled,
            backend or self._settings.rerank_backend,
            model or self._settings.rerank_model,
        )
        with self._scorer_lock:
            scorer = self._scorer_cache.get(key)
            if scorer is not None:
                return scorer
            if len(self._scorer_cache) >= 16:
                self._close_resources(self._scorer_cache.values())
                self._scorer_cache.clear()
            scorer = self._factory(*key)
            self._scorer_cache[key] = scorer
            return scorer

    @staticmethod
    def _close_resources(resources) -> None:
        closed: set[int] = set()
        first_error: BaseException | None = None
        for resource in resources:
            if resource is None or id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as exc:
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._scorer_lock:
            try:
                self._close_resources(self._scorer_cache.values())
            finally:
                self._scorer_cache.clear()

    def rank(
        self,
        command: SearchCommand,
        planned: PlannedQuery,
        recalled: RecallOutcome,
        *,
        options: RankingOptions | None = None,
        deadline: Deadline | None = None,
    ) -> RankingOutcome:
        options = options or self.resolve(command)

        default_text_scoring = self._settings.ranking_profile != "fast"
        enabled_override = (
            None
            if options.text_scoring_enabled == default_text_scoring
            else options.text_scoring_enabled
        )
        scorer = self._select_text_scorer(
            enabled_override,
            command.rerank_backend,
            command.rerank_model,
        )
        if not scorer.supports_text_scoring and options.threshold_mode != "off":
            options = options.disable_threshold("THRESHOLD_SKIPPED_NO_SCORER")

        reranker_options = {
            "profile": options.profile,
            "threshold": options.threshold,
            "threshold_mode": options.threshold_mode,
        }
        web_reranker = WebReranker(scorer, **reranker_options)
        academic_reranker = AcademicReranker(scorer, **reranker_options)
        patent_reranker = PatentReranker(scorer, **reranker_options)
        context = build_rerank_context(
            planned.search_query,
            time_sensitive=planned.plan.time_sensitive,
            reference_time=self._clock.now(),
        )
        top_k = planned.plan.top_k
        retrieved_web = [
            document
            if isinstance(document, RetrievedDocument)
            else RetrievedDocument.from_result(document, "web")
            for document in recalled.web
        ]
        retrieved_academic = [
            document
            if isinstance(document, RetrievedDocument)
            else RetrievedDocument.from_result(document, "academic")
            for document in recalled.academic
        ]
        retrieved_patent = [
            document
            if isinstance(document, RetrievedDocument)
            else RetrievedDocument.from_result(document, "patent")
            for document in recalled.patent
        ]
        web = [document.to_result() for document in retrieved_web]
        academic = [
            result
            for result in (document.to_result() for document in retrieved_academic)
            if isinstance(result, AcademicResult)
        ]
        patent = [
            result
            for result in (document.to_result() for document in retrieved_patent)
            if isinstance(result, PatentResult)
        ]

        def rank_web() -> list[SearchResult]:
            return web_reranker.rerank_with_context(
                planned.search_query, web, top_k, context
            )

        def rank_academic() -> list[AcademicResult]:
            ranked = academic_reranker.rerank_with_context(
                planned.academic_query, academic, top_k, context
            )
            return [item for item in ranked if isinstance(item, AcademicResult)]

        def rank_patent() -> list[PatentResult]:
            ranked = patent_reranker.rerank_with_context(
                planned.search_query, patent, top_k, context
            )
            return [item for item in ranked if isinstance(item, PatentResult)]

        jobs = [("web", rank_web)]
        if academic:
            jobs.append(("academic", rank_academic))
        if patent:
            jobs.append(("patent", rank_patent))

        ranked_web: list[RankedDocument] = []
        ranked_academic: list[RankedDocument] = []
        ranked_patent: list[RankedDocument] = []
        failures = []

        def source_documents(kind: DocumentKind) -> list[RetrievedDocument]:
            if kind == "academic":
                return retrieved_academic
            if kind == "patent":
                return retrieved_patent
            return retrieved_web

        def key_for(document: RetrievedDocument | SearchResult) -> str:
            url = document.url
            return normalize_url(url) or url or f"{document.title}|{document.source}"

        def attributions_for(
            result: SearchResult,
            kind: DocumentKind,
        ) -> tuple[tuple[SourceAttribution, ...], str]:
            matches = [
                document
                for document in source_documents(kind)
                if key_for(document) == key_for(result)
            ]
            if not matches:
                fallback = RetrievedDocument.from_result(result, kind)
                return fallback.attributions, fallback.content_kind
            representative = next(
                (
                    document
                    for document in matches
                    if document.content == result.content
                    and document.title == result.title
                ),
                matches[0],
            )
            unique: list[SourceAttribution] = []
            seen: set[tuple[str, str | None]] = set()
            for document in matches:
                for attribution in document.attributions:
                    identity = (attribution.provider, attribution.source_record_id)
                    if identity not in seen:
                        seen.add(identity)
                        unique.append(attribution)
            return tuple(unique), representative.content_kind

        def immutable_ranked(
            kind: DocumentKind,
            items,
        ) -> list[RankedDocument]:
            ranked_documents = []
            for result in items:
                attributions, content_kind = attributions_for(result, kind)
                ranked_documents.append(RankedDocument.from_result(
                    result,
                    kind,
                    ranking_profile=options.profile,
                    attributions=attributions,
                    content_kind=content_kind,
                ))
            return ranked_documents

        def fallback(kind: DocumentKind) -> list[RankedDocument]:
            return [
                RankedDocument(
                    document=document,
                    score=None,
                    ranking_profile=options.profile,
                )
                for document in source_documents(kind)[:top_k]
            ]

        def assign(kind: DocumentKind, items, *, already_immutable: bool = False) -> None:
            nonlocal ranked_web, ranked_academic, ranked_patent
            documents = items if already_immutable else immutable_ranked(kind, items)
            if kind == "academic":
                ranked_academic = list(documents)
            elif kind == "patent":
                ranked_patent = list(documents)
            else:
                ranked_web = list(documents)

        if deadline is not None:
            futures = {self._executor.submit(function): kind for kind, function in jobs}
            processed = set()

            def collect(future) -> None:
                kind = futures[future]
                processed.add(future)
                try:
                    assign(kind, future.result())
                except Exception as exc:
                    failures.append(search_failure(
                        stage="rerank",
                        source=f"{kind}_reranker",
                        source_type=kind,
                        code="RERANK_FAILED",
                        message=exc,
                    ))
                    assign(kind, fallback(kind), already_immutable=True)
            try:
                for future in as_completed(
                    futures, timeout=deadline.remaining_seconds()
                ):
                    collect(future)
            except TimeoutError:
                pass
            for future, kind in futures.items():
                if future in processed:
                    continue
                if future.done():
                    collect(future)
                    continue
                future.cancel()
                failures.append(search_failure(
                    stage="rerank",
                    source=f"{kind}_reranker",
                    source_type=kind,
                    code="SEARCH_DEADLINE_EXCEEDED",
                    message="search deadline exceeded",
                ))
                assign(kind, fallback(kind), already_immutable=True)
        elif len(jobs) > 1:
            futures = {self._executor.submit(function): kind for kind, function in jobs}
            for future in as_completed(futures):
                kind = futures[future]
                try:
                    assign(kind, future.result())
                except Exception as exc:
                    failures.append(search_failure(
                        stage="rerank",
                        source=f"{kind}_reranker",
                        source_type=kind,
                        code="RERANK_FAILED",
                        message=exc,
                    ))
                    assign(kind, fallback(kind), already_immutable=True)
        else:
            for kind, function in jobs:
                try:
                    assign(kind, function())
                except Exception as exc:
                    failures.append(search_failure(
                        stage="rerank",
                        source=f"{kind}_reranker",
                        source_type=kind,
                        code="RERANK_FAILED",
                        message=exc,
                    ))
                    assign(kind, fallback(kind), already_immutable=True)

        return RankingOutcome(
            web=tuple(ranked_web),
            academic=tuple(ranked_academic),
            patent=tuple(ranked_patent),
            options=options,
            reranker=scorer.name,
            failures=tuple(failures),
        )

    def resolve(self, command: SearchCommand) -> RankingOptions:
        """在执行任何检索 I/O 前解析并校验请求排序选项。"""
        return resolve_ranking_options(
            default_profile=self._settings.ranking_profile,
            default_threshold=self._settings.rerank_threshold,
            default_threshold_mode=self._settings.rerank_threshold_mode,
            ranking_profile=command.ranking_profile,
            rerank_enabled=command.rerank_enabled,
            fusion_enabled=command.fusion_enabled,
            rerank_backend=command.rerank_backend or self._settings.rerank_backend,
            rerank_threshold=command.rerank_threshold,
            rerank_threshold_mode=command.rerank_threshold_mode,
        )
