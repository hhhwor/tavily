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
    label: str = ""
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    doi: Optional[str] = None
    work_id: Optional[str] = None
    publication_number: Optional[str] = None


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


class EvidenceScores(BaseModel):
    relevance: Optional[float] = None
    source_rank: Optional[int] = None
    rerank_score: Optional[float] = None
    freshness: Optional[float] = None
    authority: Optional[float] = None
    confidence: Optional[float] = None


class EvidenceAccess(BaseModel):
    is_open: bool = False
    license: Optional[str] = None
    oa_pdf_url: Optional[str] = None
    pdf_status: Optional[str] = None
    next_cursor: Optional[str] = None


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
    type: Literal["web", "academic", "patent"]
    source: str = ""
    title: str = ""
    url: str = ""
    published_date: str = ""
    updated_date: Optional[str] = None
    language: Optional[str] = None
    passage: EvidencePassage
    citation: EvidenceCitation = Field(default_factory=EvidenceCitation)
    patent: Optional[EvidencePatent] = None
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
    type: Optional[Literal["web", "academic", "patent"]] = None
    source: Optional[str] = None


class Answerability(BaseModel):
    status: Literal["answerable", "partial", "not_answerable"] = "not_answerable"
    confidence: Literal["high", "medium", "low", "none"] = "none"
    gaps: List[AnswerabilityGap] = Field(default_factory=list)
