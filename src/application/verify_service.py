"""陈述校验应用用例。"""
from __future__ import annotations

from typing import Sequence

from src.domain.evidence import Evidence, SearchBoundary
from src.domain.trust import CandidateClaim, VerificationResult
from src.application.ports.runtime import Deadline
from src.application.research_execution import CancellationToken
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
        deadline: Deadline | None = None,
        cancellation: CancellationToken | None = None,
        use_external_models: bool = True,
    ) -> VerificationResult:
        return self.verifier.verify(
            query=query,
            claims=claims,
            evidence=evidence,
            profile=profile,
            search_boundary=search_boundary,
            deadline=deadline,
            cancellation=cancellation,
            use_external_models=use_external_models,
        )
