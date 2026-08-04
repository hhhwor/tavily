"""Research task, policy resolution and dossier contracts."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.domain.documents import DocumentKind
from src.domain.evidence import Evidence, SearchBoundary
from src.domain.failures import SearchFailure
from src.domain.trust import CandidateClaim, ClaimAssessment


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CandidateClaimInput(ResearchModel):
    text: str = Field(..., min_length=1, max_length=4000)
    importance: Literal["key", "supporting", "context"] = "key"
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    unit: str | None = None
    source: Literal["user", "agent", "extractor"] = "user"


class ResearchObjective(ResearchModel):
    question: str | None = Field(None, min_length=1, max_length=4000)
    claims: list[CandidateClaimInput] = Field(default_factory=list, max_length=20)
    required_features: list[str] = Field(default_factory=list, max_length=50)


class InputQuestion(ResearchModel):
    id: str
    field: str
    prompt: str
    kind: Literal["text", "date", "single_select", "multi_select"] = "text"
    required: bool = True
    options: list[str] = Field(default_factory=list)


class ResearchTimeScope(ResearchModel):
    from_date: date | None = Field(None, alias="from")
    to_date: date | None = Field(None, alias="to")
    basis: Literal[
        "published", "priority", "filing", "publication", "updated"
    ] = "published"

    @model_validator(mode="after")
    def validate_dates(self) -> "ResearchTimeScope":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("time.from 不能晚于 time.to")
        return self


class ResearchScope(ResearchModel):
    source_types: list[DocumentKind] | None = Field(None, min_length=1)
    time: ResearchTimeScope | None = None
    languages: list[str] = Field(default_factory=list, max_length=10)
    jurisdictions: list[str] = Field(default_factory=list, max_length=20)
    licenses: list[str] = Field(default_factory=list, max_length=20)
    required_classifications: list[str] = Field(default_factory=list, max_length=50)


class ResearchBudget(ResearchModel):
    max_rounds: int | None = Field(None, ge=1, le=10)
    max_candidates: int | None = Field(None, ge=1, le=500)
    max_deep_reads: int | None = Field(None, ge=0, le=100)
    deadline_ms: int | None = Field(None, ge=1000, le=600_000)


class ResearchPrivacy(ResearchModel):
    mode: Literal["standard", "restricted"] = "standard"
    allow_external_models: bool = True


class ResolvedResearch(ResearchModel):
    objective: ResearchObjective
    scope: ResearchScope
    profile: str
    depth: str
    policy_id: str
    policy_version: str = "1"
    budget: ResearchBudget
    privacy: ResearchPrivacy
    execution_route: Literal[
        "standard_external_allowed", "restricted_local_only"
    ] = "standard_external_allowed"
    seed_included: int = 0
    seed_excluded: int = 0
    seed_exclusion_reasons: list[str] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)


class CoverageTarget(ResearchModel):
    id: str
    dimension: Literal[
        "source_type",
        "claim",
        "required_feature",
        "time",
        "language",
        "jurisdiction",
        "classification",
        "license",
    ]
    value: str
    required: bool = True


class ObjectivePlan(ResearchModel):
    revision: int = Field(1, ge=1)
    question: str
    profile: str = ""
    claims: list[CandidateClaim] = Field(default_factory=list)
    coverage_targets: list[CoverageTarget] = Field(default_factory=list)
    ambiguities: list[InputQuestion] = Field(default_factory=list)


class ResearchAction(ResearchModel):
    id: str
    round: int = Field(..., ge=1)
    kind: Literal[
        "search",
        "counter_search",
        "deep_read",
        "citation_expand",
        "family_expand",
        "entity_expand",
    ]
    target_gap_refs: list[str] = Field(..., min_length=1)
    source_types: list[DocumentKind] = Field(default_factory=list)
    query: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    related_document_ids: list[str] = Field(default_factory=list)
    expected_gain: list[str] = Field(default_factory=list)


class ResearchProgress(ResearchModel):
    current_round: int = 0
    rounds_completed: int = 0
    raw_candidates: int = 0
    independent_works: int = 0
    patent_families: int = 0
    deep_reads: int = 0
    evidence_adopted: int = 0
    gaps_remaining: int = 0
    scope_excluded: int = 0
    scope_exclusion_reasons: list[str] = Field(default_factory=list)
    last_checkpoint_at: datetime | None = None


class ResearchUsage(ResearchModel):
    rounds: int = 0
    raw_candidates: int = 0
    adopted_candidates: int = 0
    deep_read_documents: int = 0
    deep_read_pages: int = 0
    deep_read_bytes: int = 0
    model_requests: int = 0
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    retries: int = 0
    estimated_external_cost_microunits: int = 0
    actual_external_cost_microunits: int = 0
    elapsed_ms: int = 0


class ResearchInputRequest(ResearchModel):
    id: str = "research-input"
    code: str
    message: str
    questions: list[str] = Field(default_factory=list)
    typed_questions: list[InputQuestion] = Field(default_factory=list)


class CoverageGap(ResearchModel):
    id: str
    code: str
    severity: Literal["info", "warning", "blocking"] = "warning"
    message: str
    retryable: bool = True
    suggested_action: str | None = None
    dimension: str | None = None
    value: str | None = None
    claim_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    followup_queries: list[str] = Field(default_factory=list)


class CoverageItem(ResearchModel):
    dimension: str
    value: str
    status: Literal["covered", "partial", "missing", "not_applicable"]
    evidence_refs: list[str] = Field(default_factory=list)


class ResearchCoverage(ResearchModel):
    matrix: list[CoverageItem] = Field(default_factory=list)
    gaps: list[CoverageGap] = Field(default_factory=list)
    target_met: bool = False


class CoverageGain(ResearchModel):
    new_independent_evidence: int = 0
    newly_improved_targets: list[str] = Field(default_factory=list)
    new_conflicts: int = 0
    locator_upgrades: int = 0
    score: int = 0

    @property
    def improved(self) -> bool:
        return self.score > 0


class RoundResult(ResearchModel):
    round: int = Field(..., ge=1)
    actions: list[ResearchAction] = Field(default_factory=list)
    actual_queries: list[str] = Field(default_factory=list)
    actual_filters: list[dict[str, Any]] = Field(default_factory=list)
    source_results: dict[str, int] = Field(default_factory=dict)
    failures: list[SearchFailure] = Field(default_factory=list)
    coverage_before: ResearchCoverage
    coverage_after: ResearchCoverage
    gain: CoverageGain = Field(default_factory=CoverageGain)
    usage: ResearchUsage = Field(default_factory=ResearchUsage)


class ResearchRoundCheckpoint(ResearchModel):
    research_id: str
    attempt: int = Field(..., ge=1)
    round: int = Field(..., ge=1)
    plan_revision: int = Field(..., ge=1)
    result: RoundResult
    evidence_set_revision: int = Field(..., ge=1)
    query_trace: list[str] = Field(default_factory=list)
    source_snapshot: dict[str, str] = Field(default_factory=dict)
    failures: list[SearchFailure] = Field(default_factory=list)
    scope_exclusion_reasons: list[str] = Field(default_factory=list)
    counterevidence_searched: list[str] = Field(default_factory=list)
    source_attempts: int = 0
    source_successes: int = 0
    saturation_rounds: int = 0
    usage: ResearchUsage = Field(default_factory=ResearchUsage)
    committed_at: datetime


class EvidenceFunnel(ResearchModel):
    raw_candidates: int = 0
    independent_works: int = 0
    patent_families: int = 0
    deep_reads: int = 0
    adopted: int = 0


class AssessmentDimension(ResearchModel):
    status: Literal["sufficient", "limited", "insufficient", "conflicted"]
    message: str = ""


class ResearchAssessment(ResearchModel):
    overall: Literal[
        "sufficient",
        "sufficient_with_limitations",
        "insufficient",
        "conflicted",
        "needs_expert_review",
    ] = "insufficient"
    coverage: AssessmentDimension
    independence: AssessmentDimension
    locatability: AssessmentDimension
    consistency: AssessmentDimension
    source_quality: AssessmentDimension
    reproducibility: AssessmentDimension


class ResearchFinding(ResearchModel):
    claim: CandidateClaim
    assessment: ClaimAssessment
    limitations: list[str] = Field(default_factory=list)


class ResearchDossier(ResearchModel):
    findings: list[ResearchFinding] = Field(default_factory=list)
    assessment: ResearchAssessment
    evidence_funnel: EvidenceFunnel
    coverage: ResearchCoverage
    boundaries: SearchBoundary
    evidence_index: dict[str, Evidence] = Field(default_factory=dict)
    query_trace: list[str] = Field(default_factory=list)
    plan: ObjectivePlan | None = None
    rounds: list[RoundResult] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)


class ResearchStop(ResearchModel):
    reason: Literal[
        "objective_satisfied",
        "information_gain_saturated",
        "max_rounds_reached",
        "max_candidates_reached",
        "source_failure",
        "deadline_reached",
        "queue_ttl_exceeded",
        "cancelled_by_user",
        "failed",
        "needs_input",
    ]
    message: str = ""
    remaining_gap_refs: list[str] = Field(default_factory=list)


class ResearchLinks(ResearchModel):
    self: str
    feedback: str
    cancel: str


class ResearchTaskEnvelope(ResearchModel):
    schema_version: Literal["research.v1"] = "research.v1"
    research_id: str
    state: Literal[
        "queued", "running", "completed", "partial", "needs_input", "failed", "cancelled"
    ]
    phase: Literal[
        "planning", "expanding", "deep_reading", "normalizing",
        "verifying", "coverage_analysis", "synthesizing",
    ] | None = None
    seed_search_id: str
    seed_snapshot_hash: str
    evidence_set_revision: int = 0
    task_revision: int = 0
    created_at: datetime
    updated_at: datetime
    resolved: ResolvedResearch | None = None
    progress: ResearchProgress = Field(default_factory=ResearchProgress)
    usage: ResearchUsage = Field(default_factory=ResearchUsage)
    input_request: ResearchInputRequest | None = None
    dossier: ResearchDossier | None = None
    stop: ResearchStop | None = None
    failures: list[SearchFailure] = Field(default_factory=list)
    links: ResearchLinks
    retry_after_ms: int | None = None
