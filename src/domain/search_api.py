"""Stable search.v1 response and immutable research seed snapshot."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.evidence import AnswerabilityGap, Evidence


class SearchApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchSeed(SearchApiModel):
    search_id: str
    created_at: datetime
    expires_at: datetime
    evidence_count: int
    seed_snapshot_hash: str


class RequestedFilters(SearchApiModel):
    published_from: date | None = None
    published_to: date | None = None
    languages: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)


class SourceFilterExecution(SearchApiModel):
    applied: dict[str, Any] = Field(default_factory=dict)
    post_filtered: dict[str, Any] = Field(default_factory=dict)
    unsupported: list[str] = Field(default_factory=list)
    not_applicable: list[str] = Field(default_factory=list)


class SearchQuery(SearchApiModel):
    original: str
    effective: str
    filters_requested: RequestedFilters
    filter_execution: dict[str, SourceFilterExecution] = Field(default_factory=dict)


class SearchResultSet(SearchApiModel):
    returned: int
    limit: int
    counts_by_type: dict[str, int] = Field(default_factory=dict)


class QualityMix(SearchApiModel):
    citable: int = 0
    limited: int = 0
    discovery_only: int = 0
    unavailable: int = 0


class RetrievalAssessment(SearchApiModel):
    status: Literal["usable", "limited", "unusable"] = "unusable"
    quality_mix: QualityMix = Field(default_factory=QualityMix)
    gaps: list[AnswerabilityGap] = Field(default_factory=list)


class RetrievalBoundary(SearchApiModel):
    query_time: datetime
    languages: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    license_scope: list[str] = Field(default_factory=list)
    candidate_limit: int = 0
    deadline_ms: int
    source_snapshot: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class FailureDetail(SearchApiModel):
    stage: str
    source: str = ""
    type: str | None = None
    code: str = ""
    message: str = ""
    retryable: bool = True


class SearchMeta(SearchApiModel):
    elapsed_ms: int


class SearchResponse(SearchApiModel):
    schema_version: Literal["search.v1"] = "search.v1"
    request_id: str
    status: Literal["complete", "partial"]
    research_seed: SearchSeed | None
    query: SearchQuery
    evidence: list[Evidence] = Field(default_factory=list)
    result_set: SearchResultSet
    retrieval_assessment: RetrievalAssessment
    retrieval_boundary: RetrievalBoundary
    failures: list[FailureDetail] = Field(default_factory=list)
    meta: SearchMeta


class SearchSeedSnapshot(SearchApiModel):
    """服务端持有的不可变证据与边界；不接受客户端回传。"""

    query: SearchQuery
    evidence: list[Evidence]
    retrieval_assessment: RetrievalAssessment
    retrieval_boundary: RetrievalBoundary
    failures: list[FailureDetail] = Field(default_factory=list)
