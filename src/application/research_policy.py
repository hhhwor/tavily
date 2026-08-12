"""Versioned server-side Research policy registry."""
from __future__ import annotations

from dataclasses import dataclass

from src.domain.documents import DocumentKind


class ResearchPolicyError(ValueError):
    code = "RESEARCH_POLICY_UNSATISFIABLE"


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    id: str
    version: str
    allowed_profiles: frozenset[str]
    required_source_types: frozenset[DocumentKind]
    counterevidence_required: bool
    verification_profile: str
    saturation_rounds: int = 2
    counterevidence_candidate_reserve_fraction: float = 0.25


_POLICIES = {
    "scientific-evidence.v1": ResolvedPolicy(
        id="scientific-evidence.v1",
        version="1",
        allowed_profiles=frozenset({"literature_review"}),
        required_source_types=frozenset({"academic"}),
        counterevidence_required=True,
        verification_profile="scientific",
    ),
    "technical-evidence.v1": ResolvedPolicy(
        id="technical-evidence.v1",
        version="1",
        allowed_profiles=frozenset({"technology_validation"}),
        required_source_types=frozenset(),
        counterevidence_required=True,
        verification_profile="general",
    ),
    "prior-art-evidence.v1": ResolvedPolicy(
        id="prior-art-evidence.v1",
        version="1",
        allowed_profiles=frozenset({"prior_art_landscape"}),
        required_source_types=frozenset({"patent"}),
        counterevidence_required=True,
        verification_profile="patent",
    ),
    "technical-landscape.v1": ResolvedPolicy(
        id="technical-landscape.v1",
        version="1",
        allowed_profiles=frozenset({"technology_landscape"}),
        required_source_types=frozenset(),
        counterevidence_required=False,
        verification_profile="general",
    ),
}


class ResearchPolicyRegistry:
    def resolve(self, policy_id: str, *, profile: str) -> ResolvedPolicy:
        policy = _POLICIES.get(policy_id)
        if policy is None:
            raise ResearchPolicyError(
                f"UNKNOWN_RESEARCH_POLICY: 未知 research policy: {policy_id}"
            )
        if profile not in policy.allowed_profiles:
            raise ResearchPolicyError(
                f"{ResearchPolicyError.code}: policy {policy_id} "
                f"不允许 profile {profile}"
            )
        return policy

    @staticmethod
    def ids() -> frozenset[str]:
        return frozenset(_POLICIES)
