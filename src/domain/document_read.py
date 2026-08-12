"""Original-document read results kept outside public task payloads."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.domain.documents import DocumentKind
from src.domain.evidence import EvidenceLocator


class DocumentReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentVersion(DocumentReadModel):
    """A concrete, content-addressed document version."""

    document_version_id: str
    independent_work_id: str
    type: DocumentKind
    source_record_id: str
    source_version_id: str | None = None
    canonical_uri: str = ""
    content_hash: str
    content_hash_scope: Literal[
        "full_document", "extracted_text", "observed_chunks"
    ]
    parser_version: str
    retrieved_at: str
    complete: bool = False
    etag: str | None = None
    last_modified: str | None = None
    license: str | None = None
    storage_mode: Literal["full_text", "excerpt_only", "locator_only"] = (
        "full_text"
    )
    relations: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def stable(self) -> bool:
        return self.complete and self.content_hash_scope != "observed_chunks"


class DocumentChunk(DocumentReadModel):
    document_version_id: str
    chunk_index: int = Field(..., ge=0)
    text: str
    text_hash: str
    locator: EvidenceLocator


class DocumentReadDiagnostics(DocumentReadModel):
    warnings: list[str] = Field(default_factory=list)
    failure_code: str | None = None
    message: str = ""
    retryable: bool = True
    attempts: int = Field(1, ge=1)
    retried: bool = False


class DocumentReadIntegrity(DocumentReadModel):
    """Observed completeness for a bounded original-document read.

    Counts are optional because a source may not declare its total pages or
    extracted-text length.  A partial read is never inferred as complete just
    because it has a content hash or a usable first page.
    """

    expected_pages: int | None = Field(None, ge=0)
    observed_pages: int = Field(0, ge=0)
    expected_chars: int | None = Field(None, ge=0)
    extracted_chars: int = Field(0, ge=0)
    completeness_ratio: float | None = Field(None, ge=0.0, le=1.0)
    truncation_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def derive_completeness_ratio(self) -> "DocumentReadIntegrity":
        ratios: list[float] = []
        if self.expected_pages:
            ratios.append(min(1.0, self.observed_pages / self.expected_pages))
        if self.expected_chars:
            ratios.append(min(1.0, self.extracted_chars / self.expected_chars))
        if self.completeness_ratio is None and ratios:
            self.completeness_ratio = min(ratios)
        return self


class DocumentReadResult(DocumentReadModel):
    status: Literal["ready", "partial", "failed", "unavailable"]
    version: DocumentVersion | None = None
    chunks: list[DocumentChunk] = Field(default_factory=list)
    next_cursor: str | None = None
    diagnostics: DocumentReadDiagnostics = Field(
        default_factory=DocumentReadDiagnostics
    )
    integrity: DocumentReadIntegrity = Field(
        default_factory=DocumentReadIntegrity
    )
    pages_read: int = Field(0, ge=0)
    bytes_read: int = Field(0, ge=0)

    @model_validator(mode="after")
    def validate_version_and_locators(self) -> "DocumentReadResult":
        if self.chunks and self.version is None:
            raise ValueError("document chunks require a concrete version")
        if self.status == "ready" and (
            self.version is None
            or (
                not self.chunks
                and self.version.storage_mode == "full_text"
            )
        ):
            raise ValueError("ready document reads require versioned chunks")
        if self.version is None:
            return self
        seen: set[int] = set()
        for chunk in self.chunks:
            if chunk.chunk_index in seen:
                raise ValueError("document chunk indexes must be unique")
            seen.add(chunk.chunk_index)
            if (
                chunk.document_version_id
                != self.version.document_version_id
                or chunk.locator.version_id
                != self.version.document_version_id
            ):
                raise ValueError("chunk locator/version mismatch")
            start = (
                chunk.locator.char_start
                if chunk.locator.char_start is not None else 0
            )
            end = (
                chunk.locator.char_end
                if chunk.locator.char_end is not None else len(chunk.text)
            )
            if start < 0 or end < start or end > len(chunk.text):
                raise ValueError("chunk locator character range is invalid")
        return self
