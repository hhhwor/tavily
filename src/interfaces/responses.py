"""Public response DTOs for REST, MCP reconstruction and clients."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from src.domain.evidence import Answerability, Evidence, SearchBoundary
from src.domain.failures import SearchFailure
from src.domain.trust import ClaimAssessment, TrustAssessment


class VerifyResponse(BaseModel):
    query: str
    profile: str = "general"
    assessments: List[ClaimAssessment] = Field(default_factory=list)
    trust_assessment: TrustAssessment = Field(default_factory=TrustAssessment)
    search_boundary: Optional[SearchBoundary] = None
    failures: List[SearchFailure] = Field(default_factory=list)
    elapsed_ms: int = 0


class SearchResponse(BaseModel):
    query: str
    normalized_query: str = ""
    rewritten_query: Optional[str] = None
    recency: Optional[str] = None
    time_sensitive: bool = False
    evidence: List[Evidence] = Field(default_factory=list)
    partial_failure: bool = False
    failures: List[SearchFailure] = Field(default_factory=list)
    answerability: Answerability = Field(default_factory=Answerability)
    trust_mode: Literal["off", "annotate"] = "off"
    search_boundary: Optional[SearchBoundary] = None
    count: int
    providers_used: List[str]
    reranker: str
    ranking_profile: Literal["fast", "semantic", "quality"] = "quality"
    rerank_threshold: float = 0.3
    rerank_threshold_mode: Literal["off", "prefer", "strict"] = "prefer"
    ranking_warnings: List[str] = Field(default_factory=list)
    elapsed_ms: int


class PdfTextResponse(BaseModel):
    work_id: str
    status: str
    chunk_index: Optional[int] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    text: Optional[str] = None
    returned_chars: int = 0
    next_cursor: Optional[str] = None
    partial: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
