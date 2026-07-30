"""Explicit fallback matrix for public search failures."""
from __future__ import annotations

from src.domain.search_api import DegradationDetail


_STAGE_MATRIX: dict[str, tuple[str, str]] = {
    "query_rewrite": ("use_original_query", "quality"),
    "academic_query_rewrite": ("use_original_query", "quality"),
    "routing": ("continue_available_sources", "coverage"),
    "provider_search": ("continue_available_sources", "coverage"),
    "rerank": ("use_unreranked_results", "quality"),
    "seed_store": ("omit_research_seed", "feature"),
    "pdf_enrichment": ("use_abstract_or_metadata", "quality"),
    "claim_entailment": ("use_rule_verification", "quality"),
}


def degradation_for(
    *,
    stage: str,
    code: str,
    retryable: bool,
) -> DegradationDetail:
    action, impact = _STAGE_MATRIX.get(stage, ("none", "none"))
    if code == "SEARCH_DEADLINE_EXCEEDED":
        retry_owner = "caller"
    elif stage == "seed_store" and retryable:
        retry_owner = "caller"
    elif code == "CIRCUIT_OPEN" or retryable:
        retry_owner = "server"
    else:
        retry_owner = "none"
    return DegradationDetail(
        action=action,
        impact=impact,
        retry_owner=retry_owner,
    )
