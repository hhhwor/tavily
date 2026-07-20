"""陈述校验应用用例。"""
from __future__ import annotations

from typing import Sequence

from src.domain.evidence import Evidence, SearchBoundary
from src.domain.trust import CandidateClaim, VerificationResult
from src.trust import ClaimVerifier


class VerifyService:
    def __init__(self, verifier: ClaimVerifier) -> None:
        self.verifier = verifier

    def verify(
        self,
        query: str,
        claims: Sequence[CandidateClaim],
        evidence: Sequence[Evidence],
        *,
        profile: str = "general",
        search_boundary: SearchBoundary | None = None,
    ) -> VerificationResult:
        return self.verifier.verify(
            query=query,
            claims=claims,
            evidence=evidence,
            profile=profile,
            search_boundary=search_boundary,
        )
