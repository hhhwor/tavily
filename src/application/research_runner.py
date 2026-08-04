"""Gap-driven, checkpointed Research attempt execution."""
from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence

from src.application.commands import SearchCommand, SearchFilters
from src.application.discovery_service import DiscoveryService
from src.application.document_identity import evidence_version_key
from src.application.evidence_adoption import EvidenceAdoptionGate
from src.application.evidence_assembler import EvidenceAssembler
from src.application.ports.document_reader import DocumentReader
from src.application.ports.model_router import ModelRouter, ResolvedModelRoute
from src.application.ports.research_store import ResearchStore
from src.application.ports.runtime import Clock, Deadline, DeadlineExceededError
from src.application.ports.search_seed import (
    SearchSeedIntegrityError,
    search_seed_snapshot_hash_matches,
)
from src.application.research_coverage import CoverageEvaluator, evidence_identity
from src.application.research_execution import (
    BudgetLedger,
    CancellationToken,
    ExecutionContext,
    ResearchCancelledError,
)
from src.application.research_planner import ResearchPlanner
from src.application.research_policy import ResearchPolicyRegistry, ResolvedPolicy
from src.application.research_scope import filter_evidence
from src.application.trust_annotator import TrustAnnotator
from src.application.verify_service import VerifyService
from src.domain.documents import DocumentKind
from src.domain.evidence import Evidence, SearchBoundary
from src.domain.errors import public_error_message
from src.domain.failures import SearchFailure
from src.domain.research import (
    AssessmentDimension,
    EvidenceFunnel,
    ObjectivePlan,
    ResearchAssessment,
    ResearchAction,
    ResearchCoverage,
    ResearchDossier,
    ResearchFinding,
    ResearchProgress,
    ResearchRoundCheckpoint,
    ResearchStop,
    ResearchTaskEnvelope,
    ResearchUsage,
    ResolvedResearch,
    RoundResult,
)
from src.domain.search_api import SearchSeedSnapshot
from src.domain.trust import CandidateClaim, ClaimAssessment, VerificationResult


class ResearchRunner:
    """Execute one durable attempt and checkpoint every committed round."""

    def __init__(
        self,
        *,
        task_store: ResearchStore,
        discovery: DiscoveryService,
        evidence_assembler: EvidenceAssembler,
        trust_annotator: TrustAnnotator,
        verify_service: VerifyService,
        clock: Clock,
        model_router: ModelRouter,
        policy_registry: ResearchPolicyRegistry,
        planner: ResearchPlanner,
        coverage_evaluator: CoverageEvaluator,
        document_readers: Mapping[DocumentKind, DocumentReader],
        evidence_adoption: EvidenceAdoptionGate,
    ) -> None:
        self._task_store = task_store
        self._discovery = discovery
        self._assembler = evidence_assembler
        self._trust_annotator = trust_annotator
        self._verify = verify_service
        self._clock = clock
        self._model_router = model_router
        self._policy_registry = policy_registry
        self._planner = planner
        self._coverage = coverage_evaluator
        self._document_readers = dict(document_readers)
        self._evidence_adoption = evidence_adoption

    def run(self, research_id: str) -> None:
        try:
            self._run(research_id)
        except ResearchCancelledError:
            return
        except Exception as exc:
            self._mark_failed(research_id, exc)

    def _run(self, research_id: str) -> None:
        task = self._task_store.get(research_id)
        if task.state in {"cancelled", "needs_input"}:
            return
        if self._task_store.cancel_requested(research_id):
            return
        accepted_at = task.updated_at if task.state == "queued" else task.created_at
        task = self._save_phase(task, "planning")
        resolved = task.resolved
        assert resolved is not None
        model_route = self._model_router.resolve(
            privacy=resolved.privacy,
            policy_id=resolved.policy_id,
        )
        policy = self._policy_registry.resolve(
            resolved.policy_id,
            profile=resolved.profile,
        )
        seed_snapshot = self._task_store.get_seed(research_id)
        if not search_seed_snapshot_hash_matches(
            seed_snapshot,
            task.seed_snapshot_hash,
        ):
            raise SearchSeedIntegrityError(task.seed_search_id)
        latest_plan = self._task_store.latest_plan(research_id)
        if latest_plan is None:
            attempt = 1
            plan = self._planner.build(resolved, policy)
            self._task_store.save_plan(
                research_id,
                attempt=attempt,
                plan=plan,
            )
        else:
            attempt, plan = latest_plan
        if plan.ambiguities:
            self._move_to_needs_input(task, attempt, plan)
            return

        budget = resolved.budget
        assert budget.max_rounds and budget.max_candidates is not None
        assert budget.max_deep_reads is not None and budget.deadline_ms
        queued_ms = max(
            0,
            int((self._clock.now() - accepted_at).total_seconds() * 1000),
        )
        deadline = Deadline.after(
            max(0, budget.deadline_ms - queued_ms),
            self._clock,
        )
        cancellation = CancellationToken(
            lambda: self._task_store.cancel_requested(research_id)
        )
        restored = self._task_store.latest_checkpoint(
            research_id,
            attempt=attempt,
        )
        initial_usage = (
            restored[0].usage.model_dump()
            if restored is not None
            else None
        )
        ledger = BudgetLedger(
            {
                "rounds": budget.max_rounds,
                "raw_candidates": budget.max_candidates,
                "adopted_candidates": budget.max_candidates,
                "deep_read_documents": budget.max_deep_reads,
            },
            monotonic=self._clock.monotonic,
            initial_usage=initial_usage,
        )
        context = ExecutionContext(
            research_id=research_id,
            attempt=attempt,
            policy_id=resolved.policy_id,
            privacy=resolved.privacy,
            deadline=deadline,
            cancellation=cancellation,
            budget=ledger,
        )
        if restored is None:
            seed_candidates = list(seed_snapshot.evidence)[:budget.max_candidates]
            evidence, seed_scope_reasons = filter_evidence(
                seed_candidates,
                resolved.scope,
            )
            seed_scope_reasons.extend(
                ["CANDIDATE_BUDGET_EXCLUDED"]
                * (len(seed_snapshot.evidence) - len(seed_candidates))
            )
            ledger.consume("raw_candidates", len(seed_candidates))
            ledger.consume("adopted_candidates", len(evidence))
            query_trace = [seed_snapshot.query.effective]
            snapshots = dict(
                seed_snapshot.retrieval_boundary.source_snapshot
            )
            failures: list[SearchFailure] = []
            counterevidence_searched: set[str] = set()
            source_attempts = 0
            source_successes = 0
            saturation_rounds = 0
            evidence_set_revision = task.evidence_set_revision
            history: list[RoundResult] = []
        else:
            checkpoint, evidence = restored
            query_trace = list(checkpoint.query_trace)
            snapshots = dict(checkpoint.source_snapshot)
            failures = list(checkpoint.failures)
            counterevidence_searched = set(
                checkpoint.counterevidence_searched
            )
            source_attempts = checkpoint.source_attempts
            source_successes = checkpoint.source_successes
            saturation_rounds = checkpoint.saturation_rounds
            evidence_set_revision = checkpoint.evidence_set_revision
            history = self._task_store.list_rounds(research_id)
            seed_scope_reasons = list(checkpoint.scope_exclusion_reasons)

        self._enforce_resolvable_quality(research_id, evidence)

        claims = list(plan.claims)
        deadline_reached = False
        candidate_budget_reached = False
        no_actions = False
        task = self._set_running_phase(research_id, "verifying")
        verification, deadline_hit = self._verify_current(
            resolved,
            policy,
            model_route,
            seed_snapshot,
            claims,
            evidence,
            snapshots,
            counterevidence_searched,
            context,
        )
        deadline_reached = deadline_reached or deadline_hit
        failures.extend(verification.failures)
        assessments = self._apply_counterevidence_status(
            verification.assessments,
            counterevidence_searched,
            required=policy.counterevidence_required,
        )
        coverage = self._coverage.evaluate(plan, evidence, assessments)

        while not coverage.target_met:
            if deadline_reached or deadline.expired:
                deadline_reached = True
                break
            context.checkpoint()
            if (ledger.remaining("rounds") or 0) <= 0:
                break
            round_number = ledger.used("rounds") + 1
            actions = self._planner.next_actions(
                plan,
                coverage,
                round_number=round_number,
                history=history,
                evidence=evidence,
                allow_deep_read=(
                    (ledger.remaining("deep_read_documents") or 0) > 0
                ),
            )
            if not actions:
                no_actions = True
                break
            action = actions[0]
            before_evidence = list(evidence)
            before_coverage = coverage
            before_assessments = assessments
            document_action = action.kind in {
                "deep_read", "family_expand"
            } or (
                action.kind == "citation_expand"
                and bool(action.related_document_ids)
            )
            search_action = action.kind in {"search", "counter_search"} or (
                action.kind == "citation_expand" and bool(action.query)
            )
            candidate_reservation = None
            raw_reservation = None
            if search_action:
                room = min(
                    ledger.remaining("raw_candidates") or 0,
                    ledger.remaining("adopted_candidates") or 0,
                )
                if room <= 0:
                    candidate_budget_reached = True
                    break
                candidate_reservation = ledger.reserve(
                    "adopted_candidates",
                    min(20, room),
                )
                raw_reservation = ledger.reserve(
                    "raw_candidates",
                    candidate_reservation.amount,
                )
            round_reservation = ledger.reserve("rounds")
            task = self._set_running_phase(
                research_id,
                "deep_reading" if document_action else "expanding",
            )
            try:
                if document_action:
                    (
                        new,
                        round_failures,
                        read_count,
                        read_pages,
                        read_bytes,
                    ) = self._deep_read(
                        action,
                        evidence,
                        plan,
                        before_coverage,
                        context,
                    )
                    round_raw_candidates = 0
                    round_snapshots: dict[str, str] = {}
                else:
                    assert candidate_reservation is not None
                    (
                        new,
                        round_failures,
                        read_count,
                        read_pages,
                        read_bytes,
                        round_raw_candidates,
                        round_snapshots,
                    ) = self._expand(
                        action.query or plan.question,
                        resolved,
                        limit=candidate_reservation.amount,
                        context=context,
                        model_route=model_route,
                        source_types=action.source_types or None,
                    )
            except DeadlineExceededError:
                if candidate_reservation is not None:
                    candidate_reservation.release()
                if raw_reservation is not None:
                    raw_reservation.release()
                round_reservation.release()
                deadline_reached = True
                break
            except BaseException:
                if candidate_reservation is not None:
                    candidate_reservation.release()
                if raw_reservation is not None:
                    raw_reservation.release()
                round_reservation.release()
                raise
            scoped, excluded = filter_evidence(new, resolved.scope)
            seed_scope_reasons.extend(excluded)
            if document_action:
                added = self._merge_document_evidence(evidence, scoped)
            else:
                assert candidate_reservation is not None
                assert raw_reservation is not None
                added = self._merge(evidence, scoped)
                candidate_reservation.commit(added)
                raw_reservation.commit(round_raw_candidates)
            self._enforce_resolvable_quality(research_id, evidence)
            round_reservation.commit()
            if read_count:
                ledger.consume("deep_read_documents", read_count)
            if read_pages:
                ledger.consume("deep_read_pages", read_pages)
            if read_bytes:
                ledger.consume("deep_read_bytes", read_bytes)
            failures.extend(round_failures)
            snapshots.update(round_snapshots)
            if action.query:
                query_trace.append(action.query)
            if search_action:
                source_attempts += 1
                if round_snapshots:
                    source_successes += 1
            if action.kind == "counter_search" and round_snapshots:
                gap_by_id = {gap.id: gap for gap in before_coverage.gaps}
                counterevidence_searched.update(
                    gap_by_id[gap_id].claim_ref
                    for gap_id in action.target_gap_refs
                    if gap_id in gap_by_id
                    and gap_by_id[gap_id].claim_ref is not None
                )

            task = self._set_running_phase(research_id, "verifying")
            verification, deadline_hit = self._verify_current(
                resolved,
                policy,
                model_route,
                seed_snapshot,
                claims,
                evidence,
                snapshots,
                counterevidence_searched,
                context,
            )
            deadline_reached = deadline_reached or deadline_hit
            failures.extend(verification.failures)
            assessments = self._apply_counterevidence_status(
                verification.assessments,
                counterevidence_searched,
                required=policy.counterevidence_required,
            )
            coverage = self._coverage.evaluate(plan, evidence, assessments)
            gain = self._coverage.measure_gain(
                before_coverage,
                coverage,
                before_evidence,
                evidence,
                before_assessments,
                assessments,
            )
            saturation_rounds = 0 if gain.improved else saturation_rounds + 1
            usage = ResearchUsage.model_validate(ledger.snapshot())
            result = RoundResult(
                round=round_number,
                actions=actions,
                actual_queries=[action.query] if action.query else [],
                actual_filters=(
                    [self._filter_payload(resolved)] if search_action else []
                ),
                source_results=dict(Counter(item.type for item in new)),
                failures=round_failures,
                coverage_before=before_coverage,
                coverage_after=coverage,
                gain=gain,
                usage=usage,
            )
            evidence_set_revision += 1
            checkpoint = ResearchRoundCheckpoint(
                research_id=research_id,
                attempt=attempt,
                round=round_number,
                plan_revision=plan.revision,
                result=result,
                evidence_set_revision=evidence_set_revision,
                query_trace=query_trace,
                source_snapshot=snapshots,
                failures=failures,
                scope_exclusion_reasons=seed_scope_reasons,
                counterevidence_searched=sorted(counterevidence_searched),
                source_attempts=source_attempts,
                source_successes=source_successes,
                saturation_rounds=saturation_rounds,
                usage=usage,
                committed_at=self._clock.now(),
            )
            self._task_store.checkpoint_round(checkpoint, evidence)
            history.append(result)
            task = self._save_checkpoint_progress(
                research_id,
                checkpoint,
                evidence,
                coverage,
            )
            effective_saturation = min(
                policy.saturation_rounds,
                budget.max_rounds,
            )
            if saturation_rounds >= effective_saturation:
                break

        if cancellation.cancelled:
            return
        rounds_completed = ledger.used("rounds")
        raw_candidates = ledger.used("raw_candidates")
        deep_reads = ledger.used("deep_read_documents")
        assessment = self._assessment(evidence, assessments, coverage)
        identities = {evidence_identity(item) for item in evidence}
        families = {
            item.patent.family_id or item.patent.publication_number
            for item in evidence if item.patent is not None
        }
        boundary = self._boundary(
            resolved,
            seed_snapshot,
            snapshots,
            seed_scope_reasons,
            ledger,
        )
        dossier = ResearchDossier(
            findings=[
                ResearchFinding(
                    claim=item.claim,
                    assessment=item,
                    limitations=list(item.gaps),
                )
                for item in assessments
            ],
            assessment=assessment,
            evidence_funnel=EvidenceFunnel(
                raw_candidates=raw_candidates,
                independent_works=len(identities),
                patent_families=len(families),
                deep_reads=deep_reads,
                adopted=len(evidence),
            ),
            coverage=coverage,
            boundaries=boundary,
            evidence_index={item.id: item for item in evidence},
            query_trace=query_trace,
            plan=plan,
            rounds=history,
        )
        source_failure = bool(
            source_attempts
            and source_successes == 0
            and any(
                failure.stage in {"routing", "provider_search"}
                for failure in failures
            )
        )
        if not history:
            evidence_set_revision += 1
            self._task_store.commit_evidence_set(
                research_id,
                evidence_set_revision=evidence_set_revision,
                evidence=evidence,
                committed_at=self._clock.now(),
            )
        if deadline_reached or deadline.expired:
            stop_reason = "deadline_reached"
        elif source_failure:
            stop_reason = "source_failure"
        elif coverage.target_met:
            stop_reason = "objective_satisfied"
        elif candidate_budget_reached:
            stop_reason = "max_candidates_reached"
        elif saturation_rounds >= min(
            policy.saturation_rounds,
            budget.max_rounds,
        ) or no_actions:
            stop_reason = "information_gain_saturated"
        elif rounds_completed >= budget.max_rounds:
            stop_reason = "max_rounds_reached"
        else:
            stop_reason = "information_gain_saturated"
        if stop_reason == "source_failure":
            state = "partial" if evidence else "failed"
        elif coverage.gaps and stop_reason in {
            "deadline_reached",
            "max_rounds_reached",
            "max_candidates_reached",
        }:
            state = "partial" if evidence else "failed"
        else:
            state = "completed"
        current = self._task_store.get(research_id)
        if current.state == "cancelled" or self._task_store.cancel_requested(
            research_id
        ):
            return
        final = current.model_copy(update={
            "state": state,
            "phase": None,
            "evidence_set_revision": max(
                current.evidence_set_revision,
                evidence_set_revision,
            ),
            "task_revision": current.task_revision + 1,
            "updated_at": self._clock.now(),
            "progress": self._progress(
                current_round=rounds_completed,
                evidence=evidence,
                coverage=coverage,
                raw_candidates=raw_candidates,
                deep_reads=deep_reads,
                scope_exclusion_reasons=seed_scope_reasons,
                last_checkpoint_at=current.progress.last_checkpoint_at,
            ),
            "usage": ResearchUsage.model_validate(ledger.snapshot()),
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

    def _move_to_needs_input(
        self,
        task: ResearchTaskEnvelope,
        attempt: int,
        plan: ObjectivePlan,
    ) -> None:
        current = self._task_store.get(task.research_id)
        if current.state == "cancelled":
            return
        updated = current.model_copy(update={
            "state": "needs_input",
            "phase": None,
            "task_revision": current.task_revision + 1,
            "updated_at": self._clock.now(),
            "input_request": self._planner.input_request(plan),
            "stop": ResearchStop(
                reason="needs_input",
                message="研究计划需要调用方补充输入。",
            ),
            "retry_after_ms": None,
        })
        self._task_store.save(updated, expected_revision=current.task_revision)
        self._task_store.append_event(
            task.research_id,
            attempt=attempt,
            kind="needs_input",
            payload={"plan_revision": plan.revision},
        )

    def _set_running_phase(
        self,
        research_id: str,
        phase: str,
    ) -> ResearchTaskEnvelope:
        current = self._task_store.get(research_id)
        if current.state == "cancelled":
            raise ResearchCancelledError("research execution was cancelled")
        if current.state == "running" and current.phase == phase:
            return current
        updated = current.model_copy(update={
            "state": "running",
            "phase": phase,
            "task_revision": current.task_revision + 1,
            "updated_at": self._clock.now(),
            "retry_after_ms": 1000,
        })
        return self._task_store.save(
            updated,
            expected_revision=current.task_revision,
        )

    def _save_phase(
        self,
        task: ResearchTaskEnvelope,
        phase: str,
    ) -> ResearchTaskEnvelope:
        if task.state == "running" and task.phase == phase:
            return task
        updated = task.model_copy(update={
            "state": "running",
            "phase": phase,
            "task_revision": task.task_revision + 1,
            "updated_at": self._clock.now(),
            "retry_after_ms": 1000,
        })
        return self._task_store.save(
            updated,
            expected_revision=task.task_revision,
        )

    def _save_checkpoint_progress(
        self,
        research_id: str,
        checkpoint: ResearchRoundCheckpoint,
        evidence: Sequence[Evidence],
        coverage: ResearchCoverage,
    ) -> ResearchTaskEnvelope:
        current = self._task_store.get(research_id)
        if current.state == "cancelled":
            raise ResearchCancelledError("research execution was cancelled")
        updated = current.model_copy(update={
            "state": "running",
            "phase": "coverage_analysis",
            "task_revision": current.task_revision + 1,
            "evidence_set_revision": checkpoint.evidence_set_revision,
            "updated_at": self._clock.now(),
            "progress": self._progress(
                current_round=checkpoint.round,
                evidence=evidence,
                coverage=coverage,
                raw_candidates=checkpoint.usage.raw_candidates,
                deep_reads=checkpoint.usage.deep_read_documents,
                scope_exclusion_reasons=(
                    checkpoint.scope_exclusion_reasons
                ),
                last_checkpoint_at=checkpoint.committed_at,
            ),
            "usage": checkpoint.usage,
            "retry_after_ms": 1000,
        })
        return self._task_store.save(
            updated,
            expected_revision=current.task_revision,
        )

    @staticmethod
    def _progress(
        *,
        current_round: int,
        evidence: Sequence[Evidence],
        coverage: ResearchCoverage,
        raw_candidates: int,
        deep_reads: int,
        scope_exclusion_reasons: Sequence[str],
        last_checkpoint_at,
    ) -> ResearchProgress:
        identities = {evidence_identity(item) for item in evidence}
        families = {
            item.patent.family_id or item.patent.publication_number
            for item in evidence if item.patent is not None
        }
        return ResearchProgress(
            current_round=current_round,
            rounds_completed=current_round,
            raw_candidates=raw_candidates,
            independent_works=len(identities),
            patent_families=len(families),
            deep_reads=deep_reads,
            evidence_adopted=len(evidence),
            gaps_remaining=len(coverage.gaps),
            scope_excluded=len(scope_exclusion_reasons),
            scope_exclusion_reasons=list(dict.fromkeys(
                scope_exclusion_reasons
            )),
            last_checkpoint_at=last_checkpoint_at,
        )

    def _verify_current(
        self,
        resolved: ResolvedResearch,
        policy: ResolvedPolicy,
        model_route: ResolvedModelRoute,
        seed_snapshot: SearchSeedSnapshot,
        claims: Sequence[CandidateClaim],
        evidence: Sequence[Evidence],
        snapshots: dict[str, str],
        counterevidence_searched: set[str],
        context: ExecutionContext,
    ) -> tuple[VerificationResult, bool]:
        boundary = self._boundary(
            resolved,
            seed_snapshot,
            snapshots,
            (),
            context.budget,
        )
        query = resolved.objective.question or seed_snapshot.query.original
        try:
            context.checkpoint()
            classifier = getattr(self._verify.verifier, "classifier", None)
            if (
                model_route.allow_external_models
                and bool(getattr(classifier, "is_external", False))
            ):
                context.budget.consume("model_requests")
            result = self._verify.verify(
                query,
                claims,
                evidence,
                profile=policy.verification_profile,
                search_boundary=boundary,
                deadline=context.deadline,
                cancellation=context.cancellation,
                use_external_models=model_route.allow_external_models,
            )
            return result, False
        except DeadlineExceededError:
            result = self._verify.verify(
                query,
                claims,
                evidence,
                profile=policy.verification_profile,
                search_boundary=boundary,
                cancellation=context.cancellation,
                use_external_models=False,
            )
            return result, True

    def _deep_read(
        self,
        action: ResearchAction,
        evidence: list[Evidence],
        plan: ObjectivePlan,
        coverage: ResearchCoverage,
        context: ExecutionContext,
    ) -> tuple[list[Evidence], list[SearchFailure], int, int, int]:
        candidate_by_id = {item.id: item for item in evidence}
        candidate = next((
            candidate_by_id[candidate_id]
            for candidate_id in action.candidate_ids
            if candidate_id in candidate_by_id
        ), None)
        if candidate is None:
            return (
                [],
                [SearchFailure(
                    stage="document_read",
                    source="research",
                    type=(
                        action.source_types[0]
                        if action.source_types else "web"
                    ),
                    code="DEEP_READ_CANDIDATE_MISSING",
                    message="Original-document candidate is unavailable.",
                    recoverable=False,
                )],
                0,
                0,
                0,
            )
        if action.related_document_ids:
            if candidate.type != "patent" or candidate.patent is None:
                return (
                    [],
                    [SearchFailure(
                        stage="document_read",
                        source=candidate.result_id,
                        type=candidate.type,
                        code="PATENT_RELATION_SOURCE_INVALID",
                        message="Patent relation source is invalid.",
                        recoverable=False,
                    )],
                    0,
                    0,
                    0,
                )
            candidate = self._related_patent_candidate(
                candidate,
                action.related_document_ids[0],
            )
        result = self._task_store.get_document_read(
            context.research_id,
            action_id=action.id,
        )
        must_refetch_unstored_text = bool(
            result is not None
            and result.version is not None
            and result.version.storage_mode != "full_text"
            and not result.chunks
        )
        if result is None or must_refetch_unstored_text:
            reader = self._document_readers.get(candidate.type)
            if reader is None:
                return (
                    [],
                    [SearchFailure(
                        stage="document_read",
                        source=candidate.result_id,
                        type=candidate.type,
                        code="DOCUMENT_READER_UNAVAILABLE",
                        message="Original-document reader is unavailable.",
                        recoverable=False,
                    )],
                    0,
                    0,
                    0,
                )
            result = reader.read(candidate, context=context)
            self._task_store.save_document_read(
                context.research_id,
                attempt=context.attempt,
                action_id=action.id,
                result=result,
            )

        gap_by_id = {gap.id: gap for gap in coverage.gaps}
        claim_by_id = {claim.id: claim for claim in plan.claims}
        claim_refs = {
            gap_by_id[gap_id].claim_ref
            for gap_id in action.target_gap_refs
            if gap_id in gap_by_id
            and gap_by_id[gap_id].claim_ref is not None
        }
        claim_texts = [
            claim_by_id[claim_ref].text
            for claim_ref in claim_refs
            if claim_ref in claim_by_id
        ]
        adopted = self._evidence_adoption.adopt(
            candidate,
            result,
            claim_texts=claim_texts,
        )
        if result.diagnostics.failure_code:
            failed_candidate = self._evidence_adoption.mark_failure(
                candidate,
                result,
            )
            for index, item in enumerate(evidence):
                if item.id == candidate.id:
                    evidence[index] = failed_candidate
                    break
            else:
                evidence.append(failed_candidate)
        failures: list[SearchFailure] = []
        if result.diagnostics.failure_code:
            failures.append(SearchFailure(
                stage="document_read",
                source=(
                    candidate.citation.work_id
                    or candidate.citation.doi
                    or (
                        candidate.patent.publication_number
                        if candidate.patent is not None else ""
                    )
                    or candidate.result_id
                ),
                type=candidate.type,
                code=result.diagnostics.failure_code,
                message=result.diagnostics.message,
                recoverable=result.diagnostics.retryable,
            ))
        return (
            adopted,
            failures,
            1,
            result.pages_read,
            result.bytes_read,
        )

    def _enforce_resolvable_quality(
        self,
        research_id: str,
        evidence: list[Evidence],
    ) -> None:
        """Prevent unpersisted or mismatched quotes from becoming qualified."""
        reason = "RESEARCH_LOCATOR_UNRESOLVABLE"
        for index, item in enumerate(evidence):
            quality = item.quality
            if quality is None or not quality.can_support_key_claim:
                continue
            resolved = (
                self._task_store.resolve_locator(research_id, item.locator)
                if item.locator is not None else None
            )
            if resolved == item.passage.text:
                continue
            reasons = list(dict.fromkeys([*quality.reasons, reason]))
            warnings = list(dict.fromkeys([
                *item.diagnostics.warnings,
                reason,
            ]))
            evidence[index] = item.model_copy(deep=True, update={
                "quality": quality.model_copy(update={
                    "level": "limited",
                    "has_stable_locator": False,
                    "can_support_key_claim": False,
                    "reasons": reasons,
                }),
                "diagnostics": item.diagnostics.model_copy(update={
                    "warnings": warnings,
                }),
            })

    @staticmethod
    def _related_patent_candidate(
        parent: Evidence,
        publication_number: str,
    ) -> Evidence:
        assert parent.patent is not None
        publication_number = publication_number.strip()
        clean_publication = "".join(
            character
            for character in publication_number
            if character.isalnum()
        )
        patent = parent.patent.model_copy(update={
            "publication_number": publication_number,
            "application_number": "",
            "application_date": "",
            "publication_date": "",
            "priority_root": "",
            "priority_dates": [],
            "family_members": [],
            "patent_citations": [],
            "npl_citations": [],
        })
        result_id = f"patent:{publication_number}"
        return parent.model_copy(deep=True, update={
            "id": f"{result_id}:relation",
            "result_id": result_id,
            "url": (
                f"https://patents.google.com/patent/{clean_publication}"
                if clean_publication else ""
            ),
            "passage": parent.passage.model_copy(update={
                "text": f"Related patent publication {publication_number}",
                "snippet_type": "patent_abstract",
                "char_start": None,
                "char_end": None,
                "page_from": None,
                "page_to": None,
                "chunk_index": None,
            }),
            "citation": parent.citation.model_copy(update={
                "label": publication_number,
                "publication_number": publication_number,
            }),
            "patent": patent,
            "diagnostics": parent.diagnostics.model_copy(update={
                "warnings": ["PATENT_RELATION_DISCOVERY"],
                "partial": False,
                "failure_code": None,
            }),
            "provenance": None,
            "locator": None,
            "quality": None,
        })

    def _expand(
        self,
        query: str,
        resolved: ResolvedResearch,
        *,
        limit: int,
        context: ExecutionContext,
        model_route: ResolvedModelRoute,
        source_types: Sequence[DocumentKind] | None = None,
    ) -> tuple[
        list[Evidence],
        list[SearchFailure],
        int,
        int,
        int,
        int,
        dict[str, str],
    ]:
        context.checkpoint()
        command = SearchCommand(
            query=query,
            limit=min(20, max(1, limit)),
            source_types=(
                tuple(source_types)
                if source_types is not None
                else (
                    tuple(resolved.scope.source_types)
                    if resolved.scope.source_types is not None
                    else None
                )
            ),
            filters=self._search_filters(resolved),
        )
        outcome = self._discovery.execute(
            command,
            deadline=context.deadline,
            allow_external_models=model_route.allow_external_models,
            workload_class="research",
            allow_shared_cache=resolved.privacy.mode != "restricted",
            candidate_budget=limit,
            cancellation=context.cancellation,
        )
        context.checkpoint()
        evidence = self._assembler.assemble(
            outcome.ranked.web,
            outcome.ranked.academic,
            outcome.ranked.patent,
        )[:limit]
        trust = self._trust_annotator.annotate(
            mode="annotate",
            query=query,
            planned_sources=outcome.recalled.planned_sources,
            evidence=evidence,
            query_time=outcome.query_time,
            candidate_budget=outcome.recalled.candidate_budget,
            source_snapshots={
                batch.source.id: batch.snapshot
                for batch in outcome.recalled.batches
            },
        )
        raw_candidates = sum((
            len(outcome.recalled.web),
            len(outcome.recalled.academic),
            len(outcome.recalled.patent),
        ))
        return (
            list(trust.evidence),
            [
                *outcome.planned.failures,
                *outcome.recalled.failures,
                *outcome.ranked.failures,
            ],
            0,
            0,
            0,
            raw_candidates,
            {
                batch.source.id: batch.snapshot
                for batch in outcome.recalled.batches
            },
        )

    @staticmethod
    def _search_filters(resolved: ResolvedResearch) -> SearchFilters:
        scope = resolved.scope
        published_time = (
            scope.time
            if scope.time is not None and scope.time.basis == "published"
            else None
        )
        return SearchFilters(
            published_from=(published_time.from_date if published_time else None),
            published_to=(published_time.to_date if published_time else None),
            languages=tuple(scope.languages),
            jurisdictions=tuple(scope.jurisdictions),
        )

    @classmethod
    def _filter_payload(cls, resolved: ResolvedResearch) -> dict[str, object]:
        filters = cls._search_filters(resolved)
        return {
            "published_from": (
                filters.published_from.isoformat()
                if filters.published_from else None
            ),
            "published_to": (
                filters.published_to.isoformat()
                if filters.published_to else None
            ),
            "languages": list(filters.languages),
            "jurisdictions": list(filters.jurisdictions),
        }

    @staticmethod
    def _merge(current: list[Evidence], new: Sequence[Evidence]) -> int:
        seen = {evidence_version_key(item) for item in current}
        added = 0
        for item in new:
            key = evidence_version_key(item)
            if key not in seen:
                seen.add(key)
                current.append(item)
                added += 1
        return added

    @staticmethod
    def _merge_document_evidence(
        current: list[Evidence],
        new: Sequence[Evidence],
    ) -> int:
        seen = {evidence_version_key(item) for item in current}
        added = 0
        for item in new:
            key = evidence_version_key(item)
            if key in seen:
                continue
            seen.add(key)
            current.append(item)
            added += 1
        return added

    @staticmethod
    def _apply_counterevidence_status(
        assessments: Sequence[ClaimAssessment],
        searched_claim_ids: set[str],
        *,
        required: bool = True,
    ) -> list[ClaimAssessment]:
        updated: list[ClaimAssessment] = []
        for item in assessments:
            searched = item.claim.id in searched_claim_ids
            gaps = list(item.gaps)
            if searched or not required:
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
        independent = {evidence_identity(item) for item in evidence}

        def dimension(status: str, message: str) -> AssessmentDimension:
            return AssessmentDimension(status=status, message=message)

        return ResearchAssessment(
            overall=overall,
            coverage=dimension(
                "limited" if coverage.gaps else "sufficient",
                f"剩余 {len(coverage.gaps)} 个 gap",
            ),
            independence=dimension(
                "sufficient" if len(independent) >= 2 else "insufficient",
                f"观察到 {len(independent)} 个独立来源组",
            ),
            locatability=dimension(
                "sufficient" if locatable else "insufficient",
                f"{locatable}/{len(evidence)} 条具有稳定 locator",
            ),
            consistency=dimension(
                "conflicted" if statuses["conflicted"] else "sufficient",
                f"{statuses['conflicted']} 条 claim 存在冲突",
            ),
            source_quality=dimension(
                "sufficient" if citable else "insufficient",
                f"{citable}/{len(evidence)} 条达到 citable",
            ),
            reproducibility=dimension(
                "sufficient",
                "保留 plan、round checkpoint、seed hash、查询轨迹和来源快照",
            ),
        )

    @staticmethod
    def _boundary(
        resolved: ResolvedResearch,
        seed_snapshot: SearchSeedSnapshot,
        snapshots: dict[str, str],
        scope_exclusion_reasons: Sequence[str],
        ledger: BudgetLedger,
    ) -> SearchBoundary:
        budget = resolved.budget
        limitations = list(seed_snapshot.retrieval_boundary.limitations)
        if scope_exclusion_reasons:
            limitations.append(
                f"SCOPE_POST_FILTER_EXCLUDED:{len(scope_exclusion_reasons)}"
            )
        if (
            budget.max_deep_reads == 0
            or ledger.remaining("deep_read_documents") == 0
        ):
            limitations.append("DEEP_READ_BUDGET_REACHED")
        if resolved.scope.time is not None:
            limitations.append(f"TIME_BASIS:{resolved.scope.time.basis}")
        return SearchBoundary(
            source_snapshot=snapshots,
            query_time=seed_snapshot.retrieval_boundary.query_time.isoformat(),
            languages=list(resolved.scope.languages),
            jurisdictions=list(resolved.scope.jurisdictions),
            license_scope=(
                list(resolved.scope.licenses)
                if resolved.scope.licenses
                else list(seed_snapshot.retrieval_boundary.license_scope)
            ),
            max_rounds=budget.max_rounds,
            max_candidates=budget.max_candidates,
            deadline_ms=budget.deadline_ms,
            limitations=list(dict.fromkeys(limitations)),
        )

    def expire_queued(self, research_id: str, queue_age_ms: int) -> None:
        try:
            current = self._task_store.get(research_id)
            if current.state != "queued":
                return
            expired = current.model_copy(update={
                "state": "failed",
                "phase": None,
                "task_revision": current.task_revision + 1,
                "updated_at": self._clock.now(),
                "stop": ResearchStop(
                    reason="queue_ttl_exceeded",
                    message=(
                        f"研究任务排队 {queue_age_ms}ms 后超过 queue TTL。"
                    ),
                ),
                "failures": [
                    *current.failures,
                    SearchFailure(
                        stage="research_queue",
                        source="research_dispatcher",
                        code="RESEARCH_QUEUE_TTL_EXCEEDED",
                        message=(
                            "Research queue TTL exceeded before execution."
                        ),
                        recoverable=True,
                    ),
                ],
                "retry_after_ms": None,
            })
            self._task_store.save(
                expired,
                expected_revision=current.task_revision,
            )
        except Exception:
            return

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
            self._task_store.save(
                failed,
                expected_revision=current.task_revision,
            )
        except Exception:
            return
