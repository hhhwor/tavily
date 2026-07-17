"""Build transport-ready evidence from ranked search results.

The assembler is deliberately free of provider, ranking, HTTP, and runtime
configuration concerns.  It preserves the legacy ``SearchEngine`` mapping and
cross-domain ordering while giving the application pipeline an independently
testable boundary.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Sequence

from src.models import (
    AcademicResult,
    Evidence,
    EvidenceAccess,
    EvidenceCitation,
    EvidenceDiagnostics,
    EvidencePassage,
    EvidencePatent,
    EvidenceScores,
    PatentResult,
    SearchResult,
)

DEFAULT_EVIDENCE_PASSAGE_MAX_CHARS = 1800


def _short_hash(*values: object) -> str:
    raw = "|".join(str(value or "") for value in values)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _evidence_relevance(result: SearchResult, rank: int) -> float:
    if result.rerank_score is not None:
        return float(result.rerank_score)
    return 1.0 / max(1, rank + 1)


def _citation_label(
    authors: Sequence[str],
    year: Optional[int],
    title: str,
) -> str:
    if authors:
        first = authors[0].split(",")[0].strip() or authors[0].strip()
        suffix = " et al." if len(authors) > 1 else ""
        return f"{first}{suffix}, {year}" if year else f"{first}{suffix}"
    return f"{title[:48]}, {year}" if year else title[:64]


class EvidenceAssembler:
    """Map ranked web, academic, and patent results into unified Evidence."""

    def __init__(
        self,
        passage_max_chars: int = DEFAULT_EVIDENCE_PASSAGE_MAX_CHARS,
    ) -> None:
        self._passage_max_chars = max(1, int(passage_max_chars))

    def _clip_text(self, text: str) -> tuple[str, bool]:
        normalized = (text or "").strip()
        if len(normalized) <= self._passage_max_chars:
            return normalized, False
        return (
            normalized[:self._passage_max_chars].rstrip() + "…",
            True,
        )

    def assemble(
        self,
        ranked: Sequence[SearchResult],
        ranked_papers: Sequence[AcademicResult],
        ranked_patents: Sequence[PatentResult],
    ) -> list[Evidence]:
        """Build and cross-domain sort Evidence without modifying inputs."""
        evidence: list[Evidence] = []

        for rank, result in enumerate(ranked):
            text, clipped = self._clip_text(
                result.content or result.snippet or result.title
            )
            if not text:
                continue
            result_key = _short_hash(
                "web", result.source, result.url, result.title
            )
            warnings = ["TRUNCATED_EVIDENCE"] if clipped else []
            relevance = _evidence_relevance(result, rank)
            evidence.append(Evidence(
                id=f"web:{result_key}:content",
                result_id=f"web:{result_key}",
                type="web",
                source=result.source,
                title=result.title,
                url=result.url,
                published_date=result.date,
                passage=EvidencePassage(
                    text=text,
                    snippet_type=(
                        "web_content" if result.content else "web_snippet"
                    ),
                    char_start=0,
                    char_end=len(text),
                ),
                citation=EvidenceCitation(
                    label=result.site or result.title[:64],
                    venue=result.site,
                ),
                scores=EvidenceScores(
                    relevance=relevance,
                    source_rank=rank,
                    rerank_score=result.rerank_score,
                    confidence=relevance,
                ),
                access=EvidenceAccess(is_open=bool(result.url)),
                diagnostics=EvidenceDiagnostics(
                    warnings=warnings,
                    partial=clipped,
                ),
            ))

        for rank, paper in enumerate(ranked_papers):
            result_id = (
                f"academic:{paper.work_id}"
                if paper.work_id
                else "academic:"
                + _short_hash(paper.doi, paper.url, paper.title)
            )
            source_text = (
                paper.pdf_text
                or paper.content
                or paper.snippet
                or paper.title
            )
            text, clipped = self._clip_text(source_text)
            if not text:
                continue
            snippet_type = "pdf_text" if paper.pdf_text else "abstract"
            chunk_index = (
                paper.pdf_chunk_index
                if paper.pdf_chunk_index is not None
                else 0
            )
            evidence_id = (
                f"{result_id}:pdf:{chunk_index}"
                if paper.pdf_text
                else f"{result_id}:abstract"
            )
            warnings: list[str] = []
            if clipped or paper.pdf_next_cursor:
                warnings.append("TRUNCATED_EVIDENCE")
            if (
                paper.oa_pdf_url
                and not paper.pdf_text
                and paper.pdf_status in {"not_requested", "no_pdf_url"}
            ):
                warnings.append("PDF_TEXT_UNAVAILABLE")
            if paper.pdf_error_code:
                warnings.append(paper.pdf_error_code)
            relevance = _evidence_relevance(paper, rank)
            evidence.append(Evidence(
                id=evidence_id,
                result_id=result_id,
                type="academic",
                source=paper.source,
                title=paper.title,
                url=paper.url or paper.oa_landing_url or paper.oa_pdf_url,
                published_date=paper.date or (str(paper.year) if paper.year else ""),
                language=(paper.raw or {}).get("language"),
                passage=EvidencePassage(
                    text=text,
                    snippet_type=snippet_type,
                    char_start=0,
                    char_end=len(text),
                    page_from=paper.pdf_page_from if paper.pdf_text else None,
                    page_to=paper.pdf_page_to if paper.pdf_text else None,
                    chunk_index=chunk_index if paper.pdf_text else None,
                ),
                citation=EvidenceCitation(
                    label=_citation_label(
                        paper.authors,
                        paper.year,
                        paper.title,
                    ),
                    authors=paper.authors,
                    year=paper.year,
                    venue=paper.venue,
                    doi=paper.doi or None,
                    work_id=paper.work_id or None,
                ),
                scores=EvidenceScores(
                    relevance=relevance,
                    source_rank=rank,
                    rerank_score=paper.rerank_score,
                    authority=(
                        float(paper.citations) if paper.citations else None
                    ),
                    confidence=relevance,
                ),
                access=EvidenceAccess(
                    is_open=paper.is_oa,
                    license=paper.license or None,
                    oa_pdf_url=paper.oa_pdf_url or None,
                    pdf_status=paper.pdf_status,
                    next_cursor=paper.pdf_next_cursor,
                ),
                diagnostics=EvidenceDiagnostics(
                    warnings=warnings,
                    partial=bool(clipped or paper.pdf_next_cursor),
                    failure_code=paper.pdf_error_code,
                ),
            ))

        for rank, patent in enumerate(ranked_patents):
            publication = (
                patent.publication_number
                or patent.application_number
                or _short_hash(patent.url, patent.title)
            )
            result_id = f"patent:{publication}"
            text, clipped = self._clip_text(
                patent.content or patent.snippet or patent.title
            )
            if not text:
                continue
            warnings = ["TRUNCATED_EVIDENCE"] if clipped else []
            relevance = _evidence_relevance(patent, rank)
            evidence.append(Evidence(
                id=f"{result_id}:abstract",
                result_id=result_id,
                type="patent",
                source=patent.source,
                title=patent.title,
                url=patent.url,
                published_date=(
                    patent.publication_date or patent.application_date
                ),
                passage=EvidencePassage(
                    text=text,
                    snippet_type="patent_abstract",
                    char_start=0,
                    char_end=len(text),
                ),
                citation=EvidenceCitation(
                    label=publication,
                    publication_number=patent.publication_number or None,
                ),
                patent=EvidencePatent(
                    publication_number=patent.publication_number,
                    application_number=patent.application_number,
                    applicant=patent.applicant,
                    inventor=patent.inventor,
                    ipc_main=patent.ipc_main,
                    cpc_main=patent.cpc_main,
                    country=patent.country,
                    status=patent.status,
                    family_id=patent.family_id,
                    application_date=patent.application_date,
                    publication_date=patent.publication_date,
                    patent_type=patent.patent_type,
                    citation_count=patent.citation_count,
                ),
                scores=EvidenceScores(
                    relevance=relevance,
                    source_rank=rank,
                    rerank_score=patent.rerank_score,
                    authority=(
                        float(patent.citation_count)
                        if patent.citation_count
                        else None
                    ),
                    confidence=relevance,
                ),
                access=EvidenceAccess(is_open=bool(patent.url)),
                diagnostics=EvidenceDiagnostics(
                    warnings=warnings,
                    partial=clipped,
                ),
            ))

        return sorted(
            evidence,
            key=lambda item: (
                item.scores.relevance
                if item.scores.relevance is not None
                else 0.0,
                -(item.scores.source_rank or 0),
            ),
            reverse=True,
        )
