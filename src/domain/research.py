"""Research task, policy resolution and dossier contracts."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

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
    budget: ResearchBudget
    privacy: ResearchPrivacy
    seed_included: int = 0
    seed_excluded: int = 0
    seed_exclusion_reasons: list[str] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)


class ResearchProgress(ResearchModel):
    rounds_completed: int = 0
    raw_candidates: int = 0
    independent_works: int = 0
    patent_families: int = 0
    deep_reads: int = 0
    evidence_adopted: int = 0
    gaps_remaining: int = 0


class ResearchInputRequest(ResearchModel):
    code: str
    message: str
    questions: list[str] = Field(default_factory=list)


class CoverageGap(ResearchModel):
    id: str
    code: str
    severity: Literal["info", "warning", "blocking"] = "warning"
    message: str
    retryable: bool = True
    suggested_action: str | None = None


class CoverageItem(ResearchModel):
    dimension: str
    value: str
    status: Literal["covered", "partial", "missing", "not_applicable"]
    evidence_refs: list[str] = Field(default_factory=list)


class ResearchCoverage(ResearchModel):
    matrix: list[CoverageItem] = Field(default_factory=list)
    gaps: list[CoverageGap] = Field(default_factory=list)


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
    artifacts: dict[str, str] = Field(default_factory=dict)


class ResearchStop(ResearchModel):
    reason: Literal[
        "objective_satisfied",
        "information_gain_saturated",
        "max_rounds_reached",
        "deadline_reached",
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
    input_request: ResearchInputRequest | None = None
    dossier: ResearchDossier | None = None
    stop: ResearchStop | None = None
    failures: list[SearchFailure] = Field(default_factory=list)
    links: ResearchLinks
    retry_after_ms: int | None = None
