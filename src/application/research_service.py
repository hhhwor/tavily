"""Compatibility facade for durable, gap-driven Research."""
from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence

from src.application.commands import (
    ResearchCommand,
    ResearchFeedbackCommand,
    SearchFilters,
)
from src.application.academic_document_reader import AcademicDocumentReader
from src.application.legal_document_reader import LegalDocumentReader
from src.application.discovery_service import DiscoveryService
from src.application.evidence_adoption import EvidenceAdoptionGate
from src.application.evidence_assembler import EvidenceAssembler
from src.application.model_router import (
    PrivacyAwareModelRouter,
    PrivacyPolicyUnsatisfiable,
)
from src.application.patent_document_reader import PatentDocumentReader
from src.application.ports.document_reader import DocumentReader
from src.application.ports.model_router import ModelRouter
from src.application.ports.patent_text import UnavailablePatentTextGateway
from src.application.ports.pdf_text import PdfTextGateway
from src.application.ports.research_store import (
    ResearchStore,
    StoredResearchArtifact,
)
from src.application.ports.runtime import Clock
from src.application.ports.search_seed import SearchSeedStore
from src.application.ports.synthesis import SynthesisGateway
from src.application.research_coordinator import ResearchCoordinator
from src.application.research_coverage import CoverageEvaluator
from src.application.citation_audit import CitationCoverageAuditor
from src.application.research_artifacts import ResearchArtifactService
from src.application.research_dossier_builder import StructuredDossierBuilder
from src.application.research_dispatcher import ResearchDispatcher
from src.application.research_errors import ResearchRequestError
from src.application.research_planner import ResearchPlanner
from src.application.research_policy import (
    ResearchPolicyError,
    ResearchPolicyRegistry,
)
from src.application.research_runner import ResearchRunner
from src.application.research_scope import (
    UnsupportedResearchScope,
    exclusion_reason,
    validate_scope,
)
from src.application.research_synthesis import ResearchSynthesizer
from src.application.trust_annotator import TrustAnnotator
from src.application.verify_service import VerifyService
from src.application.web_document_reader import WebDocumentReader
from src.domain.documents import DocumentKind
from src.domain.evidence import Evidence
from src.domain.research import (
    ResearchBudget,
    ResearchLinks,
    ResearchObjective,
    ResearchPrivacy,
    ResearchScope,
    ResearchTaskEnvelope,
    ResearchTimeScope,
    ResolvedResearch,
)
from src.domain.trust import CandidateClaim, ClaimAssessment
from src.infrastructure.safe_web_fetch import SafeWebFetcher


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


def _links(research_id: str) -> ResearchLinks:
    root = f"/research/{research_id}"
    return ResearchLinks(
        self=root,
        feedback=f"{root}/feedback",
        cancel=f"{root}/cancel",
    )


class ResearchService:
    """Preserve the public application API while delegating M1 responsibilities."""

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
        model_router: ModelRouter | None = None,
        policy_registry: ResearchPolicyRegistry | None = None,
        document_readers: Mapping[DocumentKind, DocumentReader] | None = None,
        synthesis_gateway: SynthesisGateway | None = None,
        artifact_retention_seconds: int = 604800,
    ) -> None:
        # Compatibility reference used by composition audits; execution is
        # owned by ResearchRunner.
        self._pdf_gateway = pdf_gateway
        self._model_router = model_router or PrivacyAwareModelRouter()
        self._policy_registry = policy_registry or ResearchPolicyRegistry()
        self._planner = ResearchPlanner()
        academic_reader = AcademicDocumentReader(pdf_gateway)
        readers: dict[DocumentKind, DocumentReader] = {
            "academic": academic_reader,
            "web": WebDocumentReader(SafeWebFetcher()),
            "patent": PatentDocumentReader(UnavailablePatentTextGateway()),
            "legal": LegalDocumentReader(),
        }
        readers.update(document_readers or {})
        self._artifact_service = ResearchArtifactService(
            store=task_store,
            clock=clock,
            retention_seconds=artifact_retention_seconds,
        )
        self._runner = ResearchRunner(
            task_store=task_store,
            discovery=discovery,
            evidence_assembler=evidence_assembler,
            trust_annotator=trust_annotator,
            verify_service=verify_service,
            clock=clock,
            model_router=self._model_router,
            policy_registry=self._policy_registry,
            planner=self._planner,
            coverage_evaluator=CoverageEvaluator(),
            document_readers=readers,
            evidence_adoption=EvidenceAdoptionGate(),
            dossier_builder=StructuredDossierBuilder(),
            synthesizer=ResearchSynthesizer(
                auditor=CitationCoverageAuditor(),
                gateway=synthesis_gateway,
            ),
            artifact_service=self._artifact_service,
        )
        self._coordinator = ResearchCoordinator(
            seed_store=seed_store,
            task_store=task_store,
            clock=clock,
            planner=self._planner,
            request_hash=self._request_hash,
            resolve=self._resolve,
            links=_links,
        )

    def attach_dispatcher(self, dispatcher: ResearchDispatcher) -> None:
        self._coordinator.attach_dispatcher(dispatcher)

    def recover_pending(self) -> None:
        """Fill newly available bounded queue slots from durable runnable tasks."""
        self._coordinator.recover_pending()

    @staticmethod
    def _request_hash(command: ResearchCommand) -> str:
        payload = {
            "search_id": command.search_id,
            "profile": command.profile,
            "depth": command.depth,
            "objective": (
                command.objective.model_dump(mode="json")
                if command.objective else None
            ),
            "scope": (
                command.scope.model_dump(mode="json", by_alias=True)
                if command.scope else None
            ),
            "policy": command.policy,
            "budget": (
                command.budget.model_dump(mode="json")
                if command.budget else None
            ),
            "privacy": (
                command.privacy.model_dump(mode="json")
                if command.privacy else None
            ),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _resolve_budget(
        depth: str,
        explicit: ResearchBudget | None,
    ) -> ResearchBudget:
        rounds, candidates, reads, deadline = _BUDGET_PRESETS[depth]
        if explicit:
            rounds = min(rounds, explicit.max_rounds or rounds)
            candidates = min(
                candidates,
                explicit.max_candidates or candidates,
            )
            reads = min(
                reads,
                explicit.max_deep_reads
                if explicit.max_deep_reads is not None
                else reads,
            )
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
        try:
            validate_scope(scope)
        except UnsupportedResearchScope as exc:
            raise ResearchRequestError(str(exc), code=exc.code) from exc
        privacy = command.privacy or ResearchPrivacy()
        adjustments: list[str] = []
        if privacy.mode == "restricted" and privacy.allow_external_models:
            privacy = privacy.model_copy(update={
                "allow_external_models": False
            })
            adjustments.append("restricted 模式已禁止外部模型处理原文")
        policy_id = command.policy or _PROFILE_POLICY[command.profile]
        try:
            policy = self._policy_registry.resolve(
                policy_id,
                profile=command.profile,
            )
        except ResearchPolicyError as exc:
            raise ResearchRequestError(str(exc), code=exc.code) from exc
        scoped_sources = set(scope.source_types or ())
        if (
            policy.required_source_types
            and scoped_sources
            and not policy.required_source_types.issubset(scoped_sources)
        ):
            missing = sorted(policy.required_source_types - scoped_sources)
            raise ResearchRequestError(
                f"{ResearchPolicyError.code}: policy {policy_id} "
                f"要求 source_types={missing}",
                code=ResearchPolicyError.code,
            )
        try:
            model_route = self._model_router.resolve(
                privacy=privacy,
                policy_id=policy_id,
            )
        except PrivacyPolicyUnsatisfiable as exc:
            raise ResearchRequestError(str(exc), code=exc.code) from exc
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
            policy_id=policy_id,
            policy_version=policy.version,
            budget=self._resolve_budget(command.depth, command.budget),
            privacy=privacy,
            execution_route=model_route.name,
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
        return self._coordinator.start(
            command,
            idempotency_key=idempotency_key,
        )

    def get(
        self,
        research_id: str,
        *,
        detail: str = "standard",
    ) -> ResearchTaskEnvelope:
        return self._coordinator.get(research_id, detail=detail)

    def feedback(
        self,
        research_id: str,
        command: ResearchFeedbackCommand,
    ) -> ResearchTaskEnvelope:
        return self._coordinator.feedback(research_id, command)

    def cancel(
        self,
        research_id: str,
        *,
        task_revision: int | None = None,
    ) -> ResearchTaskEnvelope:
        return self._coordinator.cancel(
            research_id,
            task_revision=task_revision,
        )

    def get_artifact(
        self,
        research_id: str,
        artifact_id: str,
    ) -> StoredResearchArtifact:
        # Resolve the parent first so unknown task and mismatched artifact IDs
        # share the same resource-not-found behavior.
        self._coordinator.get(research_id)
        return self._artifact_service.get(research_id, artifact_id)

    @staticmethod
    def _seed_exclusion_reason(
        item: Evidence,
        scope: ResearchScope,
    ) -> str | None:
        reason = exclusion_reason(item, scope)
        return f"SEED_{reason}" if reason is not None else None

    @staticmethod
    def _claims(resolved: ResolvedResearch) -> list[CandidateClaim]:
        return ResearchPlanner._claims(resolved)

    @staticmethod
    def _apply_counterevidence_status(
        assessments: Sequence[ClaimAssessment],
        searched_claim_ids: set[str],
        *,
        required: bool = True,
    ) -> list[ClaimAssessment]:
        return ResearchRunner._apply_counterevidence_status(
            assessments,
            searched_claim_ids,
            required=required,
        )

    @staticmethod
    def _search_filters(scope: ResearchScope) -> SearchFilters:
        published_time = (
            scope.time
            if scope.time is not None and scope.time.basis == "published"
            else None
        )
        return SearchFilters(
            published_from=(
                published_time.from_date if published_time else None
            ),
            published_to=(
                published_time.to_date if published_time else None
            ),
            languages=tuple(scope.languages),
            jurisdictions=tuple(scope.jurisdictions),
        )

    def run(self, research_id: str) -> None:
        self._runner.run(research_id)

    def expire_queued(self, research_id: str, queue_age_ms: int) -> None:
        self._runner.expire_queued(research_id, queue_age_ms)
