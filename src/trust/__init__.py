"""可信证据层；Phase 0 仅提供 provenance、locator、quality 与边界标注。"""

from src.trust.annotate import annotate_evidence, build_search_boundary
from src.trust.verifier import ClaimVerifier, build_claim_verifier

__all__ = [
    "ClaimVerifier",
    "annotate_evidence",
    "build_claim_verifier",
    "build_search_boundary",
]
