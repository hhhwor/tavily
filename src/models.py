"""Deprecated model compatibility exports.

New code imports the owning domain or interface module directly.
"""

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
from src.interfaces.responses import PdfTextResponse, SearchResponse, VerifyResponse

__all__ = [name for name in globals() if not name.startswith("_")]
