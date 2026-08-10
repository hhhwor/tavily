"""Internal structured contracts for optional Research narrative synthesis."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.domain.research import ResearchStatement


class SynthesisModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SynthesisEvidence(SynthesisModel):
    evidence_id: str
    relation: str
    quote: str
    document_id: str
    version_id: str


class SynthesisFinding(SynthesisModel):
    finding_id: str
    claim_id: str
    claim: str
    status: str
    evidence: list[SynthesisEvidence] = Field(default_factory=list)


class SynthesisRequest(SynthesisModel):
    question: str
    profile: str
    headline: str
    findings: list[SynthesisFinding] = Field(default_factory=list)
    limitation_codes: list[str] = Field(default_factory=list)


class SynthesisDraft(SynthesisModel):
    statements: list[ResearchStatement] = Field(default_factory=list)


class SynthesisGatewayResult(SynthesisModel):
    draft: SynthesisDraft
    model: str
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
