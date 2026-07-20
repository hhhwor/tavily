"""Strict inbound transport schemas shared by REST and MCP."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.application.commands import (
    ResearchCommand,
    ResearchFeedbackCommand,
    SearchCommand,
    SearchFilters,
)
from src.domain.documents import DocumentKind
from src.domain.research import (
    ResearchBudget,
    ResearchObjective,
    ResearchPrivacy,
    ResearchScope,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchFilterRequest(StrictRequest):
    published_from: date | None = None
    published_to: date | None = None
    languages: list[str] = Field(default_factory=list, max_length=10)
    jurisdictions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_dates(self) -> "SearchFilterRequest":
        if self.published_from and self.published_to:
            if self.published_from > self.published_to:
                raise ValueError("published_from 不能晚于 published_to")
        return self


class SearchRequest(StrictRequest):
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(10, ge=1, le=20)
    source_types: list[DocumentKind] | None = Field(None, min_length=1)
    filters: SearchFilterRequest = Field(default_factory=SearchFilterRequest)

    def to_command(self) -> SearchCommand:
        return SearchCommand(
            query=self.query,
            limit=self.limit,
            source_types=(
                tuple(self.source_types) if self.source_types is not None else None
            ),
            filters=SearchFilters(
                published_from=self.filters.published_from,
                published_to=self.filters.published_to,
                languages=tuple(self.filters.languages),
                jurisdictions=tuple(self.filters.jurisdictions),
            ),
        )


class ResearchRequest(StrictRequest):
    search_id: str = Field(..., min_length=1, max_length=200)
    profile: Literal[
        "literature_review",
        "technology_validation",
        "prior_art_landscape",
        "technology_landscape",
    ] = "technology_validation"
    depth: Literal["quick", "standard", "deep"] = "standard"
    objective: ResearchObjective | None = None
    scope: ResearchScope | None = None
    policy: str | None = Field(None, max_length=100)
    budget: ResearchBudget | None = None
    privacy: ResearchPrivacy | None = None

    def to_command(self) -> ResearchCommand:
        return ResearchCommand(
            search_id=self.search_id,
            profile=self.profile,
            depth=self.depth,
            objective=self.objective,
            scope=self.scope,
            policy=self.policy,
            budget=self.budget,
            privacy=self.privacy,
        )


class ResearchFeedbackRequest(StrictRequest):
    task_revision: int = Field(..., ge=0)
    answers: dict[str, str] = Field(default_factory=dict, max_length=20)
    note: str | None = Field(None, max_length=4000)

    def to_command(self) -> ResearchFeedbackCommand:
        return ResearchFeedbackCommand(
            task_revision=self.task_revision,
            answers=dict(self.answers),
            note=self.note,
        )


class ResearchCancelRequest(StrictRequest):
    task_revision: int | None = Field(None, ge=0)


ResearchDetail = Literal["standard", "full"]
