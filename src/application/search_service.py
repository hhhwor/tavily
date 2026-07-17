"""搜索用例的唯一阶段编排服务。"""
from __future__ import annotations

from src.application.answerability import AnswerabilityPolicy
from src.application.commands import SearchCommand
from src.application.evidence_assembler import EvidenceAssembler
from src.application.ports.pdf_text import PdfTextGateway
from src.application.ports.runtime import Clock, Deadline
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.source_registry import SourceRegistry
from src.application.trust_annotator import TrustAnnotator
from src.models import SearchResponse


class SearchService:
    """按固定顺序协调规划、召回、排序、富化、Evidence 与 Trust。"""

    def __init__(
        self,
        *,
        query_planner: QueryPlanner,
        recall: RecallCoordinator,
        ranking: RankingService,
        pdf_gateway: PdfTextGateway,
        evidence_assembler: EvidenceAssembler,
        trust_annotator: TrustAnnotator,
        answerability: AnswerabilityPolicy,
        source_registry: SourceRegistry,
        clock: Clock,
        deadline_ms: int,
    ) -> None:
        self._query_planner = query_planner
        self._recall = recall
        self._ranking = ranking
        self._pdf_gateway = pdf_gateway
        self._evidence_assembler = evidence_assembler
        self._trust_annotator = trust_annotator
        self._answerability = answerability
        self._source_registry = source_registry
        self._clock = clock
        self._deadline_ms = deadline_ms

    def execute(self, command: SearchCommand) -> SearchResponse:
        trust_mode = (command.trust_mode or "annotate").strip().lower()
        if trust_mode not in {"off", "annotate"}:
            raise ValueError("trust_mode 仅支持 off / annotate")

        # 排序冲突属于请求错误，必须在查询改写和外部召回之前失败。
        ranking_options = self._ranking.resolve(command)
        started = self._clock.monotonic()
        query_time = self._clock.now()
        deadline = Deadline.after(self._deadline_ms, self._clock)
        planned = self._query_planner.plan(
            command,
            self._source_registry.ids("web"),
            academic_available=self._source_registry.has_kind("academic"),
            patent_available=self._source_registry.has_kind("patent"),
        )
        recalled = self._recall.recall(planned, deadline=deadline)
        ranked = self._ranking.rank(
            command,
            planned,
            recalled,
            options=ranking_options,
            deadline=deadline,
        )
        pdf = self._pdf_gateway.enrich(
            ranked.academic,
            include_pdf_text=command.include_pdf_text,
            pdf_text_mode=command.pdf_text_mode,
            pdf_max_results=command.pdf_max_results,
            pdf_max_chars_per_result=command.pdf_max_chars_per_result,
            pdf_timeout_ms=command.pdf_timeout_ms,
            deadline=deadline,
        )

        failures = [
            *planned.failures,
            *recalled.failures,
            *ranked.failures,
            *pdf.failures,
        ]
        evidence = self._evidence_assembler.assemble(
            ranked.web,
            pdf.academic,
            ranked.patent,
        )
        trust = self._trust_annotator.annotate(
            mode=trust_mode,
            query=planned.plan.normalized_query,
            planned_sources=recalled.planned_sources,
            evidence=evidence,
            query_time=query_time,
            candidate_budget=recalled.candidate_budget,
            source_snapshots={
                batch.source.id: batch.snapshot for batch in recalled.batches
            },
        )
        answerability = self._answerability.evaluate(
            trust.evidence,
            failures,
            expected_web=bool(planned.active_provider_names),
            expected_academic=planned.plan.academic,
            expected_patent=planned.plan.patent,
            include_pdf_text=command.include_pdf_text,
        )

        return SearchResponse(
            query=command.query,
            normalized_query=planned.plan.normalized_query,
            rewritten_query=planned.plan.rewritten_query,
            recency=planned.plan.recency,
            time_sensitive=planned.plan.time_sensitive,
            evidence=list(trust.evidence),
            partial_failure=bool(failures),
            failures=failures,
            answerability=answerability,
            trust_mode=trust_mode,
            search_boundary=trust.search_boundary,
            count=len(trust.evidence),
            providers_used=list(recalled.providers_used),
            reranker=ranked.reranker,
            ranking_profile=ranked.options.profile,
            rerank_threshold=ranked.options.threshold,
            rerank_threshold_mode=ranked.options.threshold_mode,
            ranking_warnings=list(ranked.options.warnings),
            elapsed_ms=int((self._clock.monotonic() - started) * 1000),
        )
