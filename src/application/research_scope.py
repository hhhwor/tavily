"""Uniform post-retrieval scope gate for seed and newly found Evidence."""
from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from src.domain.evidence import Evidence
from src.domain.research import ResearchScope


class UnsupportedResearchScope(ValueError):
    code = "RESEARCH_SCOPE_UNSUPPORTED"


def validate_scope(scope: ResearchScope) -> None:
    if scope.time is not None and scope.time.basis == "priority":
        raise UnsupportedResearchScope(
            f"{UnsupportedResearchScope.code}: 当前 Evidence 合同没有专利优先权日，"
            "不能执行 scope.time.basis=priority"
        )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return date.fromisoformat(normalized[:10])
        except ValueError:
            return None


def _evidence_date(item: Evidence, basis: str) -> date | None:
    provenance = item.provenance
    patent = item.patent
    if basis == "updated":
        value = item.updated_date or (provenance.updated_at if provenance else None)
    elif basis == "filing":
        value = patent.application_date if patent else None
    elif basis == "publication":
        value = (
            patent.publication_date
            if patent and patent.publication_date
            else item.published_date
        )
    else:
        value = item.published_date or (
            provenance.published_at if provenance else None
        )
    return _parse_date(value)


def exclusion_reason(item: Evidence, scope: ResearchScope) -> str | None:
    if scope.source_types and item.type not in scope.source_types:
        return "SOURCE_TYPE_OUT_OF_SCOPE"
    if scope.languages and (item.language or "").casefold() not in {
        value.casefold() for value in scope.languages
    }:
        return "LANGUAGE_OUT_OF_SCOPE"
    if scope.jurisdictions and item.type == "patent":
        country = item.patent.country if item.patent else ""
        if country.upper() not in {
            value.upper() for value in scope.jurisdictions
        }:
            return "JURISDICTION_OUT_OF_SCOPE"
    if scope.licenses:
        license_id = item.access.license or (
            item.provenance.license if item.provenance else None
        )
        if (license_id or "").casefold() not in {
            value.casefold() for value in scope.licenses
        }:
            return "LICENSE_OUT_OF_SCOPE"
    if scope.required_classifications and item.type == "patent":
        classifications = {
            value
            for value in (
                item.patent.ipc_main if item.patent else "",
                item.patent.cpc_main if item.patent else "",
            )
            if value
        }
        if not any(
            actual.upper() == required.upper()
            or actual.upper().startswith(required.upper())
            for required in scope.required_classifications
            for actual in classifications
        ):
            return "CLASSIFICATION_OUT_OF_SCOPE"
    if scope.time and (scope.time.from_date or scope.time.to_date):
        observed = _evidence_date(item, scope.time.basis)
        if observed is None:
            return "DATE_UNKNOWN"
        if scope.time.from_date and observed < scope.time.from_date:
            return "DATE_OUT_OF_SCOPE"
        if scope.time.to_date and observed > scope.time.to_date:
            return "DATE_OUT_OF_SCOPE"
    return None


def filter_evidence(
    evidence: Sequence[Evidence],
    scope: ResearchScope,
) -> tuple[list[Evidence], list[str]]:
    accepted: list[Evidence] = []
    reasons: list[str] = []
    for item in evidence:
        reason = exclusion_reason(item, scope)
        if reason is None:
            accepted.append(item)
        else:
            reasons.append(reason)
    return accepted, reasons
