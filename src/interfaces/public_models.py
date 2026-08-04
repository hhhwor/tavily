"""Provider-neutral projections for every public engine response."""
from __future__ import annotations

import re
from typing import Any

from pydantic import model_serializer

from src.domain.research import ResearchTaskEnvelope
from src.domain.search_api import SearchResponse


_SNAPSHOT_LIMITATION = "SOURCE_SNAPSHOT_NOT_IMMUTABLE"


def _redact_source_from_message(message: object, source: object) -> str:
    text = str(message or "")
    source_name = str(source or "").strip()
    if not source_name:
        return text
    for name in source_name.split("+"):
        name = name.strip()
        if not name:
            continue
        variants = {name, name.removesuffix("_local")}
        for variant in variants:
            if not variant:
                continue
            text = re.sub(re.escape(variant), "upstream", text, flags=re.IGNORECASE)
            words = [part for part in re.split(r"[_\s-]+", variant) if part]
            if len(words) > 1:
                flexible = r"[_\s-]*".join(map(re.escape, words))
                text = re.sub(flexible, "upstream", text, flags=re.IGNORECASE)
    return text


def _redact_evidence(payload: dict[str, Any]) -> None:
    payload.pop("source", None)
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return
    provenance.pop("retrieved_via", None)
    field_provenance = provenance.get("field_provenance")
    if isinstance(field_provenance, dict):
        for item in field_provenance.values():
            if isinstance(item, dict):
                item.pop("retrieved_via", None)


def _redact_boundary(payload: object) -> None:
    if not isinstance(payload, dict):
        return
    payload.pop("source_snapshot", None)
    limitations = payload.get("limitations")
    if not isinstance(limitations, list):
        return
    redacted: list[object] = []
    for limitation in limitations:
        if (
            isinstance(limitation, str)
            and limitation.startswith(f"{_SNAPSHOT_LIMITATION}:")
        ):
            limitation = _SNAPSHOT_LIMITATION
        if limitation not in redacted:
            redacted.append(limitation)
    payload["limitations"] = redacted


def _redact_failures(payload: object) -> None:
    if not isinstance(payload, list):
        return
    for failure in payload:
        if not isinstance(failure, dict):
            continue
        source = failure.pop("source", "")
        if "message" in failure:
            failure["message"] = _redact_source_from_message(
                failure["message"], source
            )


def _redact_gaps(payload: object) -> None:
    if not isinstance(payload, list):
        return
    for gap in payload:
        if not isinstance(gap, dict):
            continue
        source = gap.pop("source", "")
        if "message" in gap:
            gap["message"] = _redact_source_from_message(gap["message"], source)


def _redact_search_payload(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                _redact_evidence(item)
    assessment = payload.get("retrieval_assessment")
    if isinstance(assessment, dict):
        _redact_gaps(assessment.get("gaps"))
    _redact_boundary(payload.get("retrieval_boundary"))
    _redact_failures(payload.get("failures"))
    return payload


def _redact_research_payload(payload: dict[str, Any]) -> dict[str, Any]:
    dossier = payload.get("dossier")
    if isinstance(dossier, dict):
        evidence_index = dossier.get("evidence_index")
        if isinstance(evidence_index, dict):
            for item in evidence_index.values():
                if isinstance(item, dict):
                    _redact_evidence(item)
        _redact_boundary(dossier.get("boundaries"))
        rounds = dossier.get("rounds")
        if isinstance(rounds, list):
            for item in rounds:
                if isinstance(item, dict):
                    _redact_failures(item.get("failures"))
    _redact_failures(payload.get("failures"))
    return payload


class PublicSearchResponse(SearchResponse):
    """Search response whose serialized form omits retrieval-provider identity."""

    @model_serializer(mode="wrap")
    def _serialize_public(self, handler):
        return _redact_search_payload(handler(self))


class PublicResearchTaskEnvelope(ResearchTaskEnvelope):
    """Research response whose serialized form omits retrieval-provider identity."""

    @model_serializer(mode="wrap")
    def _serialize_public(self, handler):
        return _redact_research_payload(handler(self))


def public_search_response(response: SearchResponse) -> PublicSearchResponse:
    if isinstance(response, PublicSearchResponse):
        return response
    payload = response.model_dump(mode="python")
    return PublicSearchResponse.model_validate(_redact_search_payload(payload))


def public_research_response(
    response: ResearchTaskEnvelope,
) -> PublicResearchTaskEnvelope:
    if isinstance(response, PublicResearchTaskEnvelope):
        return response
    payload = response.model_dump(mode="python")
    return PublicResearchTaskEnvelope.model_validate(
        _redact_research_payload(payload)
    )


__all__ = [
    "PublicResearchTaskEnvelope",
    "PublicSearchResponse",
    "public_research_response",
    "public_search_response",
]
