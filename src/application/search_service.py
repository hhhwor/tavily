"""Lightweight search use case and research-seed creation."""
from __future__ import annotations

import secrets
from collections import Counter

from src.application.answerability import AnswerabilityPolicy
from src.application.commands import SearchCommand
from src.application.discovery_service import DiscoveryService
from src.application.evidence_assembler import EvidenceAssembler
from src.application.ports.runtime import Clock
from src.application.ports.search_seed import SearchSeedStore
from src.application.trust_annotator import TrustAnnotator
from src.domain.documents import EnrichedDocument
from src.domain.evidence import AnswerabilityGap, Evidence
from src.domain.search_api import (
    FailureDetail,
    QualityMix,
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchMeta,
    SearchQuery,
    SearchResponse,
    SearchResultSet,
    SearchSeedSnapshot,
    SourceFilterExecution,
)


class SearchService:
    """Return discovery evidence quickly and persist its immutable research seed."""

    def __init__(
        self,
        *,
        discovery: DiscoveryService,
        evidence_assembler: EvidenceAssembler,
        trust_annotator: TrustAnnotator,
        answerability: AnswerabilityPolicy,
        seed_store: SearchSeedStore,
        clock: Clock,
        deadline_ms: int,
        seed_ttl_seconds: int,
    ) -> None:
        self._discovery = discovery
        self._evidence_assembler = evidence_assembler
        self._trust_annotator = trust_annotator
        self._answerability = answerability
        self._seed_store = seed_store
        self._clock = clock
        self._deadline_ms = deadline_ms
        self._seed_ttl_seconds = seed_ttl_seconds

    @staticmethod
    def _failure(value) -> FailureDetail:
        return FailureDetail(
            stage=value.stage,
            source=value.source,
            type=value.type,
            code=value.code,
            message=value.message,
            retryable=value.recoverable,
        )

    @staticmethod
    def _requested_filters(command: SearchCommand) -> RequestedFilters:
        filters = command.filters
        return RequestedFilters(
            published_from=filters.published_from,
            published_to=filters.published_to,
            languages=list(filters.languages),
            jurisdictions=list(filters.jurisdictions),
        )

    @staticmethod
    def _filter_execution(command: SearchCommand, batches) -> dict[str, SourceFilterExecution]:
        requested = SearchService._requested_filters(command).model_dump(
            mode="json", exclude_none=True
        )
        kinds = command.source_types or ("web", "academic", "patent")
        result: dict[str, SourceFilterExecution] = {}
        for kind in kinds:
            relevant = [batch for batch in batches if batch.source.kind == kind]
            executions = [
                batch.diagnostics.to_dict().get("applied_request_filters", {})
                for batch in relevant
            ]
            applied: dict[str, object] = {}
            unsupported: list[str] = []
            not_applicable: list[str] = []
            for name, value in requested.items():
                if value in (None, [], {}):
                    continue
                if name == "jurisdictions" and kind in {"web", "academic"}:
                    not_applicable.append(name)
                elif relevant and all(
                    execution.get(name) == value for execution in executions
                ):
                    applied[name] = value
                else:
                    unsupported.append(name)
            result[kind] = SourceFilterExecution(
                applied=applied,
                unsupported=unsupported,
                not_applicable=not_applicable,
            )
        return result

    @staticmethod
    def _quality_mix(evidence: list[Evidence]) -> QualityMix:
        counts = Counter(
            item.quality.level if item.quality is not None else "unavailable"
            for item in evidence
        )
        return QualityMix(**{
            name: counts.get(name, 0)
            for name in ("citable", "limited", "discovery_only", "unavailable")
        })

    def execute(self, command: SearchCommand) -> SearchResponse:
        started = self._clock.monotonic()
        outcome = self._discovery.execute(command)
        failures = [
            *outcome.planned.failures,
            *outcome.recalled.failures,
            *outcome.ranked.failures,
        ]
        academic = [
            EnrichedDocument.from_result(item, item.to_result())
            for item in outcome.ranked.academic
        ]
        assembled = self._evidence_assembler.assemble(
            outcome.ranked.web,
            academic,
            outcome.ranked.patent,
        )[: command.limit]
        trust = self._trust_annotator.annotate(
            mode="annotate",
            query=outcome.planned.plan.normalized_query,
            planned_sources=outcome.recalled.planned_sources,
            evidence=assembled,
            query_time=outcome.query_time,
            candidate_budget=outcome.recalled.candidate_budget,
            source_snapshots={
                batch.source.id: batch.snapshot for batch in outcome.recalled.batches
            },
        )
        evidence = list(trust.evidence)
        answerability = self._answerability.evaluate(
            evidence,
            failures,
            expected_web=bool(outcome.planned.active_provider_names),
            expected_academic=outcome.planned.do_academic,
            expected_patent=outcome.planned.do_patent,
            include_pdf_text=False,
        )
        assessment = RetrievalAssessment(
            status={
                "answerable": "usable",
                "partial": "limited",
                "not_answerable": "unusable",
            }[answerability.status],
            quality_mix=self._quality_mix(evidence),
            gaps=answerability.gaps,
        )
        filter_execution = self._filter_execution(command, outcome.recalled.batches)
        query = SearchQuery(
            original=command.query,
            effective=(
                outcome.planned.plan.rewritten_query
                or outcome.planned.plan.normalized_query
            ),
            filters_requested=self._requested_filters(command),
            filter_execution=filter_execution,
        )
        batch_limitations = sorted({
            str(item)
            for batch in outcome.recalled.batches
            for item in batch.diagnostics.to_dict().get("limitations", [])
        })
        if any(item.unsupported for item in filter_execution.values()):
            batch_limitations.append("REQUESTED_FILTER_PARTIALLY_UNSUPPORTED")
        licenses = sorted({
            str(batch.diagnostics.to_dict().get("data_license", "unspecified"))
            for batch in outcome.recalled.batches
        }) or ["unspecified"]
        boundary = RetrievalBoundary(
            query_time=outcome.query_time,
            languages=(
                list(command.filters.languages)
                or sorted({item.language for item in evidence if item.language})
            ),
            jurisdictions=list(command.filters.jurisdictions),
            license_scope=licenses,
            candidate_limit=outcome.recalled.candidate_budget,
            deadline_ms=self._deadline_ms,
            source_snapshot={
                batch.source.id: batch.snapshot for batch in outcome.recalled.batches
            },
            limitations=sorted(set(batch_limitations)),
        )
        public_failures = [self._failure(item) for item in failures]
        snapshot = SearchSeedSnapshot(
            query=query,
            evidence=evidence,
            retrieval_assessment=assessment,
            retrieval_boundary=boundary,
            failures=public_failures,
        )
        seed = None
        try:
            seed = self._seed_store.save(
                snapshot,
                ttl_seconds=self._seed_ttl_seconds,
            )
        except Exception:
            public_failures.append(FailureDetail(
                stage="seed_store",
                source="search_seed_store",
                code="SEARCH_SEED_UNAVAILABLE",
                message="研究种子暂时不可用；搜索结果仍可使用。",
                retryable=True,
            ))

        counts = Counter(item.type for item in evidence)
        return SearchResponse(
            request_id="req_" + secrets.token_urlsafe(12),
            status="partial" if public_failures else "complete",
            research_seed=seed,
            query=query,
            evidence=evidence,
            result_set=SearchResultSet(
                returned=len(evidence),
                limit=command.limit,
                counts_by_type=dict(counts),
            ),
            retrieval_assessment=assessment,
            retrieval_boundary=boundary,
            failures=public_failures,
            meta=SearchMeta(
                elapsed_ms=int((self._clock.monotonic() - started) * 1000)
            ),
        )
