"""Claim verification domain contracts."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from src.domain.evidence import EvidenceLocator, SearchBoundary
from src.domain.failures import SearchFailure


class CandidateClaim(BaseModel):
    id: str
    text: str
    claim_type: str = "factual"
    importance: Literal["key", "supporting", "context"] = "key"
    subject: Optional[str] = None
    predicate: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    time_scope: Optional[str] = None
    jurisdiction: Optional[str] = None
    source: Literal["user", "agent", "extractor"] = "agent"
    parent_id: Optional[str] = None


class ConsistencyCheck(BaseModel):
    name: str
    status: Literal["pass", "fail", "unknown"] = "unknown"
    claim_value: Optional[str] = None
    evidence_value: Optional[str] = None
    reason: str = ""


class ClaimEvidenceRelation(BaseModel):
    evidence_id: str
    relation: Literal[
        "supports", "contradicts", "mentions", "unclear", "irrelevant"
    ]
    confidence: Literal["high", "medium", "low", "none"] = "none"
    reason: str = ""
    quote: str = ""
    locator: Optional[EvidenceLocator] = None
    evidence_quality: str = "unavailable"
    qualified: bool = False
    consistency_checks: List[ConsistencyCheck] = Field(default_factory=list)


class ClaimAssessment(BaseModel):
    claim: CandidateClaim
    status: Literal[
        "supported", "conflicted", "insufficient", "inference", "needs_expert_review"
    ] = "insufficient"
    confidence: Literal["high", "medium", "low", "none"] = "none"
    relations: List[ClaimEvidenceRelation] = Field(default_factory=list)
    support_refs: List[str] = Field(default_factory=list)
    conflict_refs: List[str] = Field(default_factory=list)
    mention_refs: List[str] = Field(default_factory=list)
    independent_support_count: int = 0
    primary_source_count: int = 0
    counterevidence_searched: bool = False
    gaps: List[str] = Field(default_factory=list)
    followup_queries: List[str] = Field(default_factory=list)
    review_required: bool = False


class TrustAssessment(BaseModel):
    status: Literal["supported", "mixed", "insufficient"] = "insufficient"
    claims_total: int = 0
    supported_claims: int = 0
    conflicted_claims: int = 0
    insufficient_claims: int = 0
    evidence_coverage_rate: float = 0.0
    unsupported_statement_rate: float = 1.0
    policy_version: str = "trust-phase1-v1"
    model: str = "rules"
    warnings: List[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """Internal verification stage result; it is not a public API response."""

    query: str
    profile: str = "general"
    assessments: List[ClaimAssessment] = Field(default_factory=list)
    trust_assessment: TrustAssessment = Field(default_factory=TrustAssessment)
    search_boundary: Optional[SearchBoundary] = None
    failures: List[SearchFailure] = Field(default_factory=list)
    elapsed_ms: int = 0
