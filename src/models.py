"""Convenience exports for current domain contracts."""

from src.domain.evidence import (
    Answerability,
    AnswerabilityGap,
    Evidence,
    EvidenceAccess,
    EvidenceCitation,
    EvidenceDiagnostics,
    EvidenceFieldProvenance,
    EvidenceLocator,
    EvidencePassage,
    EvidencePatent,
    EvidenceProvenance,
    EvidenceQuality,
    EvidenceScores,
    SearchBoundary,
)
from src.domain.failures import SearchFailure
from src.domain.search import AcademicResult, PatentResult, SearchPlan, SearchResult
from src.domain.trust import (
    CandidateClaim,
    ClaimAssessment,
    ClaimEvidenceRelation,
    ConsistencyCheck,
    TrustAssessment,
)
from src.domain.research import ResearchTaskEnvelope
from src.domain.search_api import SearchResponse

__all__ = [name for name in globals() if not name.startswith("_")]
