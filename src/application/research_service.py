"""Durable multi-round research task orchestration."""
from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from datetime import datetime
from typing import Callable, Sequence

from src.application.commands import (
    ResearchCommand,
    ResearchFeedbackCommand,
    SearchCommand,
    SearchFilters,
)
from src.application.discovery_service import DiscoveryService
from src.application.evidence_assembler import EvidenceAssembler
from src.application.ports.pdf_text import PdfTextGateway
from src.application.ports.research_store import ResearchStore
from src.application.ports.runtime import Clock, Deadline
from src.application.ports.search_seed import (
    SearchSeedIntegrityError,
    SearchSeedStore,
    search_seed_snapshot_hash_matches,
)
from src.application.research_dispatcher import ResearchDispatcher
from src.application.trust_annotator import TrustAnnotator
from src.application.verify_service import VerifyService
from src.domain.documents import EnrichedDocument
from src.domain.evidence import Evidence, SearchBoundary
from src.domain.errors import public_error_message
from src.domain.failures import SearchFailure
from src.domain.research import (
    AssessmentDimension,
    CoverageGap,
    CoverageItem,
    EvidenceFunnel,
    ResearchAssessment,
    ResearchBudget,
    ResearchCoverage,
    ResearchDossier,
    ResearchFinding,
    ResearchLinks,
    ResearchObjective,
    ResearchPrivacy,
    ResearchProgress,
    ResearchScope,
    ResearchStop,
    ResearchTaskEnvelope,
    ResearchTimeScope,
    ResolvedResearch,
)
from src.domain.trust import CandidateClaim, ClaimAssessment


_BUDGET_PRESETS = {
    "quick": (1, 30, 2, 30_000),
    "standard": (3, 100, 10, 120_000),
    "deep": (5, 250, 30, 300_000),
}
_PROFILE_POLICY = {
    "literature_review": "scientific-evidence.v1",
    "technology_validation": "technical-evidence.v1",
    "prior_art_landscape": "prior-art-evidence.v1",
    "technology_landscape": "technical-landscape.v1",
}
_VERIFY_PROFILE = {
    "literature_review": "scientific",
    "technology_validation": "general",
    "prior_art_landscape": "patent",
    "technology_landscape": "general",
}


class ResearchRequestError(ValueError):
    """The requested policy/scope cannot be accepted as submitted."""


def _links(research_id: str) -> ResearchLinks:
    root = f"/research/{research_id}"
    return ResearchLinks(
        self=root,
        feedback=f"{root}/feedback",
        cancel=f"{root}/cancel",
    )


class ResearchService:
    def __init__(
        self,
        *,
        seed_store: SearchSeedStore,
        task_store: ResearchStore,
        discovery: DiscoveryService,
        evidence_assembler: EvidenceAssembler,
        trust_annotator: TrustAnnotator,
        pdf_gateway: PdfTextGateway,
        verify_service: VerifyService,
        clock: Clock,
    ) -> None:
        self._seed_store = seed_store
        self._task_store = task_store
        self._discovery = discovery
        self._assembler = evidence_assembler
        self._trust_annotator = trust_annotator
        self._pdf_gateway = pdf_gateway
        self._verify = verify_service
        self._clock = clock
        self._dispatcher: ResearchDispatcher | None = None

    def attach_dispatcher(self, dispatcher: ResearchDispatcher) -> None:
        self._dispatcher = dispatcher

    @staticmethod
    def _request_hash(command: ResearchCommand) -> str:
        payload = {
            "search_id": command.search_id,
            "profile": command.profile,
            "depth": command.depth,
            "objective": command.objective.model_dump(mode="json") if command.objective else None,
            "scope": command.scope.model_dump(mode="json", by_alias=True) if command.scope else None,
            "policy": command.policy,
            "budget": command.budget.model_dump(mode="json") if command.budget else None,
            "privacy": command.privacy.model_dump(mode="json") if command.privacy else None,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _resolve_budget(depth: str, explicit: ResearchBudget | None) -> ResearchBudget:
        rounds, candidates, reads, deadline = _BUDGET_PRESETS[depth]
        if explicit:
            rounds = min(rounds, explicit.max_rounds or rounds)
            candidates = min(candidates, explicit.max_candidates or candidates)
            reads = min(reads, explicit.max_deep_reads if explicit.max_deep_reads is not None else reads)
            deadline = min(deadline, explicit.deadline_ms or deadline)
        return ResearchBudget(
            max_rounds=rounds,
            max_candidates=candidates,
            max_deep_reads=reads,
            deadline_ms=deadline,
        )

    @staticmethod
    def _resolve_scope(command: ResearchCommand, seed) -> ResearchScope:
        if command.scope is not None:
            return command.scope
        filters = seed.snapshot.query.filters_requested
        time_scope = None
        if filters.published_from or filters.published_to:
            time_scope = ResearchTimeScope(
                from_date=filters.published_from,
                to_date=filters.published_to,
            )
        snapshot = seed.snapshot
        if snapshot.requested_source_types is not None:
            source_types = list(snapshot.requested_source_types)
        elif snapshot.planned_source_types:
            source_types = list(snapshot.planned_source_types)
        else:
            # 兼容没有来源意图字段的旧 seed。缺失来源 gap 代表该来源原本被
            # 规划过，不能因为最终 Top-K 没留下该类型就把 research 锁死。
            source_types = [item.type for item in snapshot.evidence]
            source_types.extend(
                gap.type
                for gap in snapshot.retrieval_assessment.gaps
                if gap.type is not None
            )
        source_types = list(dict.fromkeys(source_types))
        return ResearchScope(
            source_types=source_types or None,
            time=time_scope,
            languages=list(filters.languages),
            jurisdictions=list(filters.jurisdictions),
        )

    def _resolve(self, command: ResearchCommand, seed) -> ResolvedResearch:
        objective = command.objective or ResearchObjective(
            question=seed.snapshot.query.original
        )
        if not objective.question:
            objective = objective.model_copy(update={
                "question": seed.snapshot.query.original
            })
        scope = self._resolve_scope(command, seed)
        privacy = command.privacy or ResearchPrivacy()
        adjustments: list[str] = []
        if privacy.mode == "restricted" and privacy.allow_external_models:
            privacy = privacy.model_copy(update={"allow_external_models": False})
            adjustments.append("restricted 模式已禁止外部模型处理原文")
        if command.policy and command.policy not in set(_PROFILE_POLICY.values()):
            raise ResearchRequestError(f"未知 research policy: {command.policy}")
        exclusion_reasons = [
            reason
            for item in seed.snapshot.evidence
            if (reason := self._seed_exclusion_reason(item, scope)) is not None
        ]
        included = len(seed.snapshot.evidence) - len(exclusion_reasons)
        return ResolvedResearch(
            objective=objective,
            scope=scope,
            profile=command.profile,
            depth=command.depth,
            policy_id=command.policy or _PROFILE_POLICY[command.profile],
            budget=self._resolve_budget(command.depth, command.budget),
            privacy=privacy,
            seed_included=included,
            seed_excluded=len(seed.snapshot.evidence) - included,
            seed_exclusion_reasons=list(dict.fromkeys(exclusion_reasons)),
            adjustments=adjustments,
        )

    def start(
        self,
        command: ResearchCommand,
        *,
        idempotency_key: str,
    ) -> ResearchTaskEnvelope:
        if self._dispatcher is None:
            raise RuntimeError("Research dispatcher 尚未装配")
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 200:
            raise ResearchRequestError(
                "Idempotency-Key 必须是 1 到 200 个字符的非空值"
            )
        request_hash = self._request_hash(command)
        existing = self._task_store.find_by_idempotency(
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing
        seed = self._seed_store.get(command.search_id)
        now = self._clock.now()
        research_id = "rsch_" + secrets.token_urlsafe(18)
        task = ResearchTaskEnvelope(
            research_id=research_id,
            state="queued",
            phase="planning",
            seed_search_id=seed.seed.search_id,
            seed_snapshot_hash=seed.seed.seed_snapshot_hash,
            created_at=now,
            updated_at=now,
            resolved=self._resolve(command, seed),
            links=_links(research_id),
            retry_after_ms=500,
        )
        stored, created = self._task_store.create(
            task,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            seed_snapshot=seed.snapshot,
        )
        if created:
            self._dispatcher.submit(stored.research_id)
        return stored

    def get(self, research_id: str, *, detail: str = "standard") -> ResearchTaskEnvelope:
        task = self._task_store.get(research_id)
        if detail == "full" or task.dossier is None:
            return task
        refs = {
            ref
            for finding in task.dossier.findings
            for ref in (
                finding.assessment.support_refs
                + finding.assessment.conflict_refs
                + finding.assessment.mention_refs
            )
        }
        dossier = task.dossier.model_copy(update={
            "evidence_index": {
                key: item
                for key, item in task.dossier.evidence_index.items()
                if key in refs
            },
            "query_trace": [],
        })
        return task.model_copy(update={"dossier": dossier})

    def feedback(
        self,
        research_id: str,
        command: ResearchFeedbackCommand,
    ) -> ResearchTaskEnvelope:
        if self._dispatcher is None:
            raise RuntimeError("Research dispatcher 尚未装配")
        current = self._task_store.get(research_id)
        if current.task_revision != command.task_revision:
            raise ValueError("task_revision 已过期")
        if current.state != "needs_input":
            raise ValueError("只有 needs_input 状态可以提交 feedback")
        resolved = current.resolved
        assert resolved is not None
        note = command.note or "; ".join(
            f"{key}: {value}" for key, value in command.answers.items()
        )
        resolved = resolved.model_copy(update={
            "adjustments": [*resolved.adjustments, f"用户反馈: {note}"],
        })
        updated = current.model_copy(update={
            "state": "queued",
            "phase": "planning",
            "task_revision": current.task_revision + 1,
            "updated_at": self._clock.now(),
            "resolved": resolved,
            "input_request": None,
            "stop": None,
            "retry_after_ms": 500,
        })
        saved = self._task_store.save(updated, expected_revision=current.task_revision)
        self._dispatcher.submit(research_id)
        return saved

    def cancel(self, research_id: str, *, task_revision: int | None = None) -> ResearchTaskEnvelope:
        current = self._task_store.get(research_id)
        if task_revision is not None and current.task_revision != task_revision:
            raise ValueError("task_revision 已过期")
        if current.state in {"completed", "partial", "failed", "cancelled"}:
            return current
        updated = current.model_copy(update={
            "state": "cancelled",
            "phase": None,
            "task_revision": current.task_revision + 1,
            "updated_at": self._clock.now(),
            "stop": ResearchStop(
                reason="cancelled_by_user",
                message="研究任务已由调用方取消。",
            ),
            "retry_after_ms": None,
        })
        return self._task_store.cancel(
            updated,
            expected_revision=current.task_revision,
        )

    @staticmethod
    def _seed_exclusion_reason(item: Evidence, scope: ResearchScope) -> str | None:
        if scope.source_types and item.type not in scope.source_types:
            return "SEED_SOURCE_TYPE_OUT_OF_SCOPE"
        if scope.languages and item.language not in scope.languages:
            return "SEED_LANGUAGE_OUT_OF_SCOPE"
        if scope.jurisdictions and item.type == "patent":
            country = item.patent.country if item.patent else ""
            if country not in scope.jurisdictions:
                return "SEED_JURISDICTION_OUT_OF_SCOPE"
        if scope.licenses:
            license_id = item.access.license or (
                item.provenance.license if item.provenance else None
            )
            if license_id not in scope.licenses:
                return "SEED_LICENSE_OUT_OF_SCOPE"
        if scope.time and (scope.time.from_date or scope.time.to_date):
            try:
                published = datetime.fromisoformat(
                    item.published_date[:10]
                ).date()
            except (TypeError, ValueError):
                return "SEED_DATE_UNKNOWN"
            if scope.time.from_date and published < scope.time.from_date:
                return "SEED_DATE_OUT_OF_SCOPE"
            if scope.time.to_date and published > scope.time.to_date:
                return "SEED_DATE_OUT_OF_SCOPE"
        return None

    @staticmethod
    def _identity(item: Evidence) -> str:
        if item.type == "academic":
            return "academic:" + str(item.citation.doi or item.citation.work_id or item.result_id)
        if item.type == "patent" and item.patent is not None:
            return "patent:" + str(
                item.patent.family_id
                or item.patent.publication_number
                or item.result_id
            )
        if item.provenance is not None:
            return "web:" + str(
                item.provenance.canonical_url
                or item.provenance.document_id
                or item.result_id
            )
        return item.result_id

    @classmethod
    def _merge(cls, current: list[Evidence], new: Sequence[Evidence]) -> int:
        seen = {cls._identity(item) for item in current}
        added = 0
        for item in new:
            key = cls._identity(item)
            if key not in seen:
                seen.add(key)
                current.append(item)
                added += 1
        return added

    @staticmethod
    def _claims(resolved: ResolvedResearch) -> list[CandidateClaim]:
        if resolved.objective.claims:
            return [
                CandidateClaim(
                    id=f"claim_{index + 1}",
                    text=item.text,
                    importance=item.importance,
                    subject=item.subject,
                    predicate=item.predicate,
                    value=item.value,
                    unit=item.unit,
                    source=item.source,
                )
                for index, item in enumerate(resolved.objective.claims)
            ]
        return [CandidateClaim(
            id="claim_1",
            text=resolved.objective.question or "",
            claim_type="research_question",
            source="agent",
        )]

    @staticmethod
    def _apply_counterevidence_status(
        assessments: Sequence[ClaimAssessment],
        searched_claim_ids: set[str],
    ) -> list[ClaimAssessment]:
        updated: list[ClaimAssessment] = []
        for item in assessments:
            searched = item.claim.id in searched_claim_ids
            gaps = list(item.gaps)
            if searched:
                gaps = [
                    gap for gap in gaps
                    if gap != "COUNTEREVIDENCE_NOT_SEARCHED"
                ]
            updated.append(item.model_copy(update={
                "counterevidence_searched": searched,
                "gaps": gaps,
            }))
        return updated

    @staticmethod
    def _search_filters(scope: ResearchScope) -> SearchFilters:
        return SearchFilters(
            published_from=scope.time.from_date if scope.time else None,
            published_to=scope.time.to_date if scope.time else None,
            languages=tuple(scope.languages),
            jurisdictions=tuple(scope.jurisdictions),
        )

    def _expand(
        self,
        query: str,
        resolved: ResolvedResearch,
        *,
        limit: int,
        deep_reads_left: int,
        deadline: Deadline,
    ) -> tuple[list[Evidence], list[SearchFailure], int, dict[str, str]]:
        command = SearchCommand(
            query=query,
            limit=min(20, max(1, limit)),
            source_types=(
                tuple(resolved.scope.source_types)
                if resolved.scope.source_types is not None else None
            ),
            filters=self._search_filters(resolved.scope),
        )
        outcome = self._discovery.execute(command, deadline=deadline)
        pdf = self._pdf_gateway.enrich(
            outcome.ranked.academic,
            include_pdf_text=deep_reads_left > 0,
            pdf_text_mode="sync",
            pdf_max_results=deep_reads_left,
            deadline=deadline,
        )
        evidence = self._assembler.assemble(
            outcome.ranked.web,
            pdf.academic,
            outcome.ranked.patent,
        )[:limit]
        trust = self._trust_annotator.annotate(
            mode="annotate",
            query=query,
            planned_sources=outcome.recalled.planned_sources,
            evidence=evidence,
            query_time=outcome.query_time,
            candidate_budget=outcome.recalled.candidate_budget,
            source_snapshots={batch.source.id: batch.snapshot for batch in outcome.recalled.batches},
        )
        deep_reads = sum(
            1 for item in trust.evidence if item.passage.snippet_type == "pdf_text"
        )
        return (
            list(trust.evidence),
            [
                *outcome.planned.failures,
                *outcome.recalled.failures,
                *outcome.ranked.failures,
                *pdf.failures,
            ],
            deep_reads,
            {batch.source.id: batch.snapshot for batch in outcome.recalled.batches},
        )

    @staticmethod
    def _coverage(
        resolved: ResolvedResearch,
        evidence: Sequence[Evidence],
        assessments: Sequence[ClaimAssessment],
    ) -> ResearchCoverage:
        matrix: list[CoverageItem] = []
        for source_type in resolved.scope.source_types or ["web", "academic", "patent"]:
            refs = [item.id for item in evidence if item.type == source_type]
            matrix.append(CoverageItem(
                dimension="source_type",
                value=source_type,
                status="covered" if refs else "missing",
                evidence_refs=refs,
            ))
        searchable = " ".join(
            f"{item.title} {item.passage.text}" for item in evidence
        ).lower()
        for feature in resolved.objective.required_features:
            refs = [
                item.id for item in evidence
                if feature.lower() in f"{item.title} {item.passage.text}".lower()
            ]
            matrix.append(CoverageItem(
                dimension="required_feature",
                value=feature,
                status="covered" if refs else "missing",
                evidence_refs=refs,
            ))
        for language in resolved.scope.languages:
            refs = [item.id for item in evidence if item.language == language]
            matrix.append(CoverageItem(
                dimension="language",
                value=language,
                status="covered" if refs else "missing",
                evidence_refs=refs,
            ))
        for jurisdiction in resolved.scope.jurisdictions:
            refs = [
                item.id for item in evidence
                if item.patent is not None and item.patent.country == jurisdiction
            ]
            matrix.append(CoverageItem(
                dimension="jurisdiction",
                value=jurisdiction,
                status="covered" if refs else "missing",
                evidence_refs=refs,
            ))
        for classification in resolved.scope.required_classifications:
            refs = [
                item.id for item in evidence
                if item.patent is not None and classification in {
                    item.patent.ipc_main, item.patent.cpc_main
                }
            ]
            matrix.append(CoverageItem(
                dimension="classification",
                value=classification,
                status="covered" if refs else "missing",
                evidence_refs=refs,
            ))
        gaps: list[CoverageGap] = []
        for index, assessment in enumerate(assessments):
            for gap in assessment.gaps:
                gaps.append(CoverageGap(
                    id=f"gap_claim_{index + 1}_{len(gaps) + 1}",
                    code=gap,
                    severity="blocking" if assessment.claim.importance == "key" else "warning",
                    message=f"{assessment.claim.id}: {gap}",
                    suggested_action=(
                        assessment.followup_queries[0]
                        if assessment.followup_queries else "补充可定位的一手证据"
                    ),
                ))
        for item in matrix:
            if item.status == "missing":
                gaps.append(CoverageGap(
                    id=f"gap_coverage_{len(gaps) + 1}",
                    code="COVERAGE_MISSING",
                    message=f"未覆盖 {item.dimension}: {item.value}",
                    suggested_action=f"围绕 {item.value} 执行补充检索",
                ))
        return ResearchCoverage(matrix=matrix, gaps=gaps)

    @staticmethod
    def _assessment(
        evidence: Sequence[Evidence],
        assessments: Sequence[ClaimAssessment],
        coverage: ResearchCoverage,
    ) -> ResearchAssessment:
        statuses = Counter(item.status for item in assessments)
        if statuses["conflicted"]:
            overall = "conflicted"
        elif statuses["needs_expert_review"]:
            overall = "needs_expert_review"
        elif statuses["insufficient"]:
            overall = "insufficient"
        elif coverage.gaps:
            overall = "sufficient_with_limitations"
        else:
            overall = "sufficient"
        citable = sum(
            1 for item in evidence
            if item.quality is not None and item.quality.level == "citable"
        )
        locatable = sum(
            1 for item in evidence
            if item.quality is not None and item.quality.has_stable_locator
        )
        independent = {
            (
                item.provenance.ownership_group
                or item.provenance.publisher_id
                or item.provenance.document_id
            )
            for item in evidence if item.provenance is not None
        }
        dimension = lambda status, message: AssessmentDimension(status=status, message=message)
        return ResearchAssessment(
            overall=overall,
            coverage=dimension("limited" if coverage.gaps else "sufficient", f"剩余 {len(coverage.gaps)} 个 gap"),
            independence=dimension("sufficient" if len(independent) >= 2 else "insufficient", f"观察到 {len(independent)} 个独立来源组"),
            locatability=dimension("sufficient" if locatable else "insufficient", f"{locatable}/{len(evidence)} 条具有稳定 locator"),
            consistency=dimension("conflicted" if statuses["conflicted"] else "sufficient", f"{statuses['conflicted']} 条 claim 存在冲突"),
            source_quality=dimension("sufficient" if citable else "insufficient", f"{citable}/{len(evidence)} 条达到 citable"),
            reproducibility=dimension("sufficient", "保留 seed hash、查询轨迹和来源快照"),
        )

    def run(self, research_id: str) -> None:
        try:
            task = self._task_store.get(research_id)
            if task.state == "cancelled" or self._task_store.cancel_requested(research_id):
                return
            running = task.model_copy(update={
                "state": "running",
                "phase": "expanding",
                "task_revision": task.task_revision + 1,
                "updated_at": self._clock.now(),
                "retry_after_ms": 1000,
            })
            task = self._task_store.save(running, expected_revision=task.task_revision)
            resolved = task.resolved
            assert resolved is not None
            seed_snapshot = self._task_store.get_seed(research_id)
            if not search_seed_snapshot_hash_matches(
                seed_snapshot,
                task.seed_snapshot_hash,
            ):
                raise SearchSeedIntegrityError(task.seed_search_id)
            evidence = [
                item for item in seed_snapshot.evidence
                if self._seed_exclusion_reason(item, resolved.scope) is None
            ]
            raw_candidates = len(evidence)
            query_trace = [seed_snapshot.query.effective]
            snapshots = dict(seed_snapshot.retrieval_boundary.source_snapshot)
            failures: list[SearchFailure] = []
            rounds_completed = 0
            deep_reads = 0
            budget = resolved.budget
            assert budget.max_rounds and budget.max_candidates is not None
            assert budget.max_deep_reads is not None and budget.deadline_ms
            deadline = Deadline.after(budget.deadline_ms, self._clock)
            claims = self._claims(resolved)
            expansion_queries = [
                (
                    f"{claim.text} 反例 争议 limitation counter evidence",
                    claim.id,
                )
                for claim in claims if claim.importance == "key"
            ]
            if resolved.objective.question:
                expansion_queries.append((resolved.objective.question, None))
            counterevidence_searched: set[str] = set()
            information_gain_saturated = False
            for query, counter_claim_id in dict.fromkeys(expansion_queries):
                if rounds_completed >= budget.max_rounds or deadline.expired:
                    break
                if self._task_store.cancel_requested(research_id):
                    return
                room = budget.max_candidates - len(evidence)
                if room <= 0:
                    break
                new, round_failures, read_count, round_snapshots = self._expand(
                    query,
                    resolved,
                    limit=min(20, room),
                    deep_reads_left=max(0, budget.max_deep_reads - deep_reads),
                    deadline=deadline,
                )
                raw_candidates += len(new)
                added = self._merge(evidence, new)
                failures.extend(round_failures)
                deep_reads += read_count
                snapshots.update(round_snapshots)
                query_trace.append(query)
                rounds_completed += 1
                if counter_claim_id is not None and round_snapshots:
                    counterevidence_searched.add(counter_claim_id)
                if added == 0:
                    information_gain_saturated = not round_failures
                    break

            if self._task_store.cancel_requested(research_id):
                return
            verify_boundary = SearchBoundary(
                source_snapshot=snapshots,
                query_time=seed_snapshot.retrieval_boundary.query_time.isoformat(),
                languages=list(resolved.scope.languages),
                jurisdictions=list(resolved.scope.jurisdictions),
                license_scope=list(seed_snapshot.retrieval_boundary.license_scope),
                max_rounds=budget.max_rounds,
                max_candidates=budget.max_candidates,
                deadline_ms=budget.deadline_ms,
                limitations=list(seed_snapshot.retrieval_boundary.limitations),
            )
            verification = self._verify.verify(
                resolved.objective.question or seed_snapshot.query.original,
                claims,
                evidence,
                profile=_VERIFY_PROFILE[resolved.profile],
                search_boundary=verify_boundary,
            )
            failures.extend(verification.failures)
            assessments = self._apply_counterevidence_status(
                verification.assessments,
                counterevidence_searched,
            )
            coverage = self._coverage(resolved, evidence, assessments)
            assessment = self._assessment(evidence, assessments, coverage)
            identities = {self._identity(item) for item in evidence}
            families = {
                item.patent.family_id or item.patent.publication_number
                for item in evidence if item.patent is not None
            }
            funnel = EvidenceFunnel(
                raw_candidates=raw_candidates,
                independent_works=len(identities),
                patent_families=len(families),
                deep_reads=deep_reads,
                adopted=len(evidence),
            )
            dossier = ResearchDossier(
                findings=[
                    ResearchFinding(
                        claim=item.claim,
                        assessment=item,
                        limitations=list(item.gaps),
                    ) for item in assessments
                ],
                assessment=assessment,
                evidence_funnel=funnel,
                coverage=coverage,
                boundaries=verify_boundary,
                evidence_index={item.id: item for item in evidence},
                query_trace=query_trace,
            )
            if deadline.expired:
                stop_reason = "deadline_reached"
            elif assessment.overall == "sufficient":
                stop_reason = "objective_satisfied"
            elif information_gain_saturated:
                stop_reason = "information_gain_saturated"
            elif rounds_completed >= budget.max_rounds and coverage.gaps:
                stop_reason = "max_rounds_reached"
            else:
                stop_reason = "information_gain_saturated"
            state = (
                "partial"
                if coverage.gaps and stop_reason in {"deadline_reached", "max_rounds_reached"}
                else "completed"
            )
            current = self._task_store.get(research_id)
            if current.state == "cancelled" or self._task_store.cancel_requested(research_id):
                return
            final = current.model_copy(update={
                "state": state,
                "phase": None,
                "evidence_set_revision": current.evidence_set_revision + 1,
                "task_revision": current.task_revision + 1,
                "updated_at": self._clock.now(),
                "progress": ResearchProgress(
                    rounds_completed=rounds_completed,
                    raw_candidates=raw_candidates,
                    independent_works=len(identities),
                    patent_families=len(families),
                    deep_reads=deep_reads,
                    evidence_adopted=len(evidence),
                    gaps_remaining=len(coverage.gaps),
                ),
                "dossier": dossier,
                "stop": ResearchStop(
                    reason=stop_reason,
                    message=(
                        f"研究在 {rounds_completed} 轮扩展后停止；"
                        f"结论状态为 {assessment.overall}。"
                    ),
                    remaining_gap_refs=[item.id for item in coverage.gaps],
                ),
                "failures": failures,
                "retry_after_ms": None,
            })
            self._task_store.save(final, expected_revision=current.task_revision)
        except Exception as exc:
            self._mark_failed(research_id, exc)

    def _mark_failed(self, research_id: str, error: Exception) -> None:
        try:
            current = self._task_store.get(research_id)
            if current.state == "cancelled":
                return
            failed = current.model_copy(update={
                "state": "failed",
                "phase": None,
                "task_revision": current.task_revision + 1,
                "updated_at": self._clock.now(),
                "stop": ResearchStop(
                    reason="failed",
                    message="研究任务执行失败。",
                ),
                "failures": [
                    *current.failures,
                    SearchFailure(
                        stage="research",
                        source="research_worker",
                        code="RESEARCH_FAILED",
                        message=public_error_message(error),
                        recoverable=True,
                    ),
                ],
                "retry_after_ms": None,
            })
            self._task_store.save(failed, expected_revision=current.task_revision)
        except Exception:
            return
