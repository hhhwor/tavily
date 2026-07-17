"""Search candidate DTOs and immutable query plan."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from src.domain.failures import SearchFailure


class SearchResult(BaseModel):
    """Mutable compatibility DTO used only at provider/ranking adapter boundaries."""

    url: str
    title: str = ""
    snippet: str = ""
    content: str = ""
    date: str = ""
    site: str = ""
    score: Optional[float] = None
    rerank_score: Optional[float] = None
    provider_rank: Optional[int] = None
    source: str = ""
    raw: Dict[str, Any] = Field(default_factory=dict, repr=False)

    def text_for_rerank(self) -> str:
        body = self.content or self.snippet or ""
        return f"{self.title}\n{body}".strip()


class AcademicResult(SearchResult):
    authors: List[str] = Field(default_factory=list)
    work_id: str = ""
    year: Optional[int] = None
    venue: str = ""
    citations: int = 0
    doi: str = ""
    oa_url: str = ""
    oa_landing_url: str = ""
    oa_pdf_url: str = ""
    license: str = ""
    license_id: str = ""
    is_oa: bool = False
    oa_status: str = ""
    topic: str = ""
    pdf_status: str = "not_requested"
    pdf_text: str = ""
    pdf_pages: Optional[int] = None
    pdf_text_length: int = 0
    pdf_returned_chars: int = 0
    pdf_chunk_index: Optional[int] = None
    pdf_page_from: Optional[int] = None
    pdf_page_to: Optional[int] = None
    pdf_next_cursor: Optional[str] = None
    pdf_error_code: Optional[str] = None
    pdf_error_message: Optional[str] = None


class PatentResult(SearchResult):
    publication_number: str = ""
    application_number: str = ""
    applicant: List[str] = Field(default_factory=list)
    inventor: List[str] = Field(default_factory=list)
    ipc_main: str = ""
    cpc_main: str = ""
    application_date: str = ""
    publication_date: str = ""
    patent_type: str = ""
    country: str = ""
    status: str = ""
    family_id: str = ""
    citation_count: int = 0


class SearchPlan(BaseModel):
    """Frozen L0 plan consumed by application coordination."""

    model_config = ConfigDict(frozen=True)

    raw_query: str
    normalized_query: str
    rewritten_query: Optional[str] = None
    recency: Optional[str] = None
    time_sensitive: bool = False
    academic: bool = False
    patent: bool = False
    providers: Tuple[str, ...] = ()
    top_k: int = 10
    failures: Tuple[SearchFailure, ...] = ()
