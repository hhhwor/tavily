"""Versioned inbound transport schemas."""
from __future__ import annotations

from dataclasses import fields
from typing import Any, List, Literal, Mapping, Optional

from pydantic import BaseModel, Field, model_validator

from src.application.commands import SearchCommand
from src.models import CandidateClaim, Evidence, SearchBoundary
from src.pipeline.ranking_options import resolve_ranking_options


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="搜索查询")
    top_k: int = Field(0, ge=0, le=50, description="返回条数,0 用默认")
    include_academic: Optional[bool] = Field(
        None,
        description="学术论文检索:None=自动,true=强制开,false=强制关",
    )
    include_patent: Optional[bool] = Field(
        None,
        description="专利检索:None=自动,true=强制开,false=强制关",
    )
    include_pdf_text: bool = False
    pdf_text_mode: Optional[Literal["cached", "sync"]] = Field(
        None, description="PDF 正文模式: cached / sync"
    )
    pdf_max_results: Optional[int] = Field(None, ge=0, le=5)
    pdf_max_chars_per_result: Optional[int] = Field(None, ge=1, le=30000)
    pdf_timeout_ms: Optional[int] = Field(None, ge=1000, le=60000)
    ranking_profile: Optional[Literal["fast", "semantic", "quality"]] = None
    rerank_enabled: Optional[bool] = Field(
        None,
        json_schema_extra={"deprecated": True},
    )
    rerank_threshold: Optional[float] = Field(None, ge=0, le=1)
    rerank_threshold_mode: Optional[Literal["off", "prefer", "strict"]] = None
    fusion_enabled: Optional[bool] = Field(
        None,
        json_schema_extra={"deprecated": True},
    )
    rewrite_enabled: Optional[bool] = None
    trust_mode: Literal["off", "annotate"] = "annotate"

    @model_validator(mode="before")
    @classmethod
    def reject_runtime_model_overrides(cls, value: Any) -> Any:
        if isinstance(value, dict):
            forbidden = sorted(
                field
                for field in ("rerank_backend", "rerank_model")
                if field in value
            )
            if forbidden:
                raise ValueError(
                    "请求不允许覆盖重排后端或模型；请使用 ranking_profile"
                )
        return value

    @model_validator(mode="after")
    def validate_explicit_ranking_options(self) -> "SearchRequest":
        resolve_ranking_options(
            default_profile="quality",
            default_threshold=0.3,
            default_threshold_mode="prefer",
            ranking_profile=self.ranking_profile,
            rerank_enabled=self.rerank_enabled,
            fusion_enabled=self.fusion_enabled,
            rerank_threshold=self.rerank_threshold,
            rerank_threshold_mode=self.rerank_threshold_mode,
        )
        return self

    def to_command(self) -> SearchCommand:
        return search_command_from_mapping(self.model_dump())


def search_command_from_mapping(
    values: Mapping[str, Any],
    *,
    aliases: Mapping[str, str] | None = None,
) -> SearchCommand:
    """Filter transport-local values into the authoritative command contract."""
    data = dict(values)
    for alias, target in (aliases or {}).items():
        if alias in data and target not in data:
            data[target] = data[alias]
    allowed = {field.name for field in fields(SearchCommand)}
    return SearchCommand(**{
        key: value for key, value in data.items() if key in allowed
    })


class VerifyRequest(BaseModel):
    query: str = Field(..., min_length=1)
    claims: List[CandidateClaim] = Field(..., min_length=1, max_length=20)
    evidence: List[Evidence] = Field(..., max_length=100)
    profile: Literal[
        "general", "news", "scientific", "patent", "legal", "financial", "product"
    ] = "general"
    search_boundary: Optional[SearchBoundary] = None
