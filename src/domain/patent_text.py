"""Normalized patent claims, specification and relationship records."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PatentTextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatentTextUnit(PatentTextModel):
    kind: Literal["claim", "description"]
    identifier: str
    text: str
    section: str | None = None


class PatentDocumentRecord(PatentTextModel):
    status: Literal["ready", "unavailable", "failed"]
    publication_number: str
    application_number: str = ""
    family_id: str = ""
    priority_root: str = ""
    priority_dates: list[str] = Field(default_factory=list)
    application_date: str = ""
    publication_date: str = ""
    canonical_uri: str = ""
    units: list[PatentTextUnit] = Field(default_factory=list)
    family_members: list[str] = Field(default_factory=list)
    patent_citations: list[str] = Field(default_factory=list)
    npl_citations: list[str] = Field(default_factory=list)
    source_version_id: str | None = None
    license: str | None = None
    failure_code: str | None = None
    retryable: bool = True
