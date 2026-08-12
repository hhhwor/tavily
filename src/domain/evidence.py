"""Evidence units, provenance, locators and answerability models."""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class EvidencePassage(BaseModel):
    text: str
    snippet_type: str = ""
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    chunk_index: Optional[int] = None


class EvidenceCitation(BaseModel):
    """Citation identity and public link state for one evidence item.

    ``link_status`` deliberately reports only what the search pipeline can
    establish without fetching the original document.  It must not be used as
    a substitute for ``EvidenceQuality`` or for claim verification.
    """

    label: str = ""
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    doi: Optional[str] = None
    work_id: Optional[str] = None
    publication_number: Optional[str] = None
    source_url: str = ""
    canonical_url: str = ""
    link_status: Literal[
        "citable", "traceable", "missing", "invalid"
    ] = "missing"


class EvidencePatent(BaseModel):
    publication_number: str = ""
    application_number: str = ""
    applicant: List[str] = Field(default_factory=list)
    inventor: List[str] = Field(default_factory=list)
    ipc_main: str = ""
    cpc_main: str = ""
    country: str = ""
    status: str = ""
    family_id: str = ""
    application_date: str = ""
    publication_date: str = ""
    patent_type: str = ""
    citation_count: int = 0
    priority_root: str = ""
    priority_dates: List[str] = Field(default_factory=list)
    family_members: List[str] = Field(default_factory=list)
    patent_citations: List[str] = Field(default_factory=list)
    npl_citations: List[str] = Field(default_factory=list)


class EvidenceLegal(BaseModel):
    """法规检索返回的结构化定位与效力信息。"""

    law_type: str = ""
    status: str = ""
    department: str = ""
    directory: List[str] = Field(default_factory=list)
    item: str = ""


class EvidenceScores(BaseModel):
    relevance: Optional[float] = None
    source_rank: Optional[int] = None
    rerank_score: Optional[float] = None
    freshness: Optional[float] = None
    authority: Optional[float] = None
    confidence: Optional[float] = None


class EvidenceFulltext(BaseModel):
    """Public summary of an original-document read and its completeness."""

    status: Literal[
        "not_requested", "ready", "partial", "failed", "unavailable"
    ] = "not_requested"
    source_url: str = ""
    expected_pages: Optional[int] = None
    observed_pages: int = 0
    expected_chars: Optional[int] = None
    extracted_chars: int = 0
    completeness_ratio: Optional[float] = None
    truncation_reasons: List[str] = Field(default_factory=list)
    attempts: int = 0


class EvidenceAccess(BaseModel):
    is_open: bool = False
    license: Optional[str] = None
    oa_pdf_url: Optional[str] = None
    pdf_status: Optional[str] = None
    original_status: Optional[str] = None
    next_cursor: Optional[str] = None
    fulltext: EvidenceFulltext = Field(default_factory=EvidenceFulltext)


class EvidenceDiagnostics(BaseModel):
    warnings: List[str] = Field(default_factory=list)
    partial: bool = False
    failure_code: Optional[str] = None


class SearchBoundary(BaseModel):
    source_snapshot: Dict[str, str] = Field(default_factory=dict)
    query_time: str
    languages: List[str] = Field(default_factory=list)
    jurisdictions: List[str] = Field(default_factory=list)
    license_scope: List[str] = Field(default_factory=list)
    max_rounds: int = 1
    max_candidates: int = 0
    deadline_ms: Optional[int] = None
    limitations: List[str] = Field(default_factory=list)


class EvidenceFieldProvenance(BaseModel):
    source_field: Optional[str] = None
    retrieved_via: str = ""
    transformations: List[str] = Field(default_factory=list)


class EvidenceProvenance(BaseModel):
    source_url: str = ""
    canonical_url: str = ""
    publisher_id: str = ""
    publisher_name: str = ""
    publisher_type: str = "unknown"
    retrieved_via: str = ""
    content_origin: str = "unknown"
    document_id: str = ""
    version_id: Optional[str] = None
    source_record_id: Optional[str] = None
    published_at: Optional[str] = None
    updated_at: Optional[str] = None
    retrieved_at: str
    ownership_group: Optional[str] = None
    syndication_group: Optional[str] = None
    license: Optional[str] = None
    original_language: Optional[str] = None
    parser_version: Optional[str] = None
    ocr_used: bool = False
    translation_used: bool = False
    field_provenance: Dict[str, EvidenceFieldProvenance] = Field(default_factory=dict)


class EvidenceLocator(BaseModel):
    document_id: str
    version_id: Optional[str] = None
    section: Optional[str] = None
    subsection: Optional[str] = None
    paragraph_id: Optional[str] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    table_id: Optional[str] = None
    figure_id: Optional[str] = None
    claim_number: Optional[str] = None
    chunk_index: Optional[int] = None


class EvidenceQuality(BaseModel):
    level: Literal["citable", "limited", "discovery_only", "unavailable"] = "unavailable"
    is_original: bool = False
    has_stable_locator: bool = False
    can_support_key_claim: bool = False
    reasons: List[str] = Field(default_factory=list)


class Evidence(BaseModel):
    id: str
    result_id: str
    type: Literal["web", "academic", "patent", "legal"]
    source: str = ""
    title: str = ""
    url: str = ""
    published_date: str = ""
    updated_date: Optional[str] = None
    language: Optional[str] = None
    passage: EvidencePassage
    citation: EvidenceCitation = Field(default_factory=EvidenceCitation)
    patent: Optional[EvidencePatent] = None
    legal: Optional[EvidenceLegal] = None
    scores: EvidenceScores = Field(default_factory=EvidenceScores)
    access: EvidenceAccess = Field(default_factory=EvidenceAccess)
    diagnostics: EvidenceDiagnostics = Field(default_factory=EvidenceDiagnostics)
    provenance: Optional[EvidenceProvenance] = None
    locator: Optional[EvidenceLocator] = None
    quality: Optional[EvidenceQuality] = None


class AnswerabilityGap(BaseModel):
    code: str
    severity: Literal["info", "warning", "blocking"] = "warning"
    message: str
    type: Optional[Literal["web", "academic", "patent", "legal"]] = None
    source: Optional[str] = None


class Answerability(BaseModel):
    status: Literal["answerable", "partial", "not_answerable"] = "not_answerable"
    confidence: Literal["high", "medium", "low", "none"] = "none"
    gaps: List[AnswerabilityGap] = Field(default_factory=list)
