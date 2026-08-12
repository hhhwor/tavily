"""Adopt short, resolvable excerpts from original document reads."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from src.domain.document_read import DocumentChunk, DocumentReadResult
from src.domain.evidence import (
    Evidence,
    EvidenceDiagnostics,
    EvidenceFulltext,
    EvidenceLocator,
    EvidencePassage,
    EvidenceProvenance,
    EvidenceQuality,
)
from src.trust.entailment import normalize_text, text_tokens


@dataclass(frozen=True, slots=True)
class _Excerpt:
    chunk: DocumentChunk
    start: int
    end: int
    score: int


class EvidenceAdoptionGate:
    def __init__(
        self,
        *,
        max_excerpt_chars: int = 1800,
        max_passages: int = 5,
    ) -> None:
        self._max_excerpt_chars = max(1, max_excerpt_chars)
        self._max_passages = max(1, max_passages)

    def adopt(
        self,
        candidate: Evidence,
        result: DocumentReadResult,
        *,
        claim_texts: Sequence[str] = (),
    ) -> list[Evidence]:
        version = result.version
        if version is None or not result.chunks:
            return []
        excerpts = sorted(
            (
                self._select_excerpt(chunk, claim_texts)
                for chunk in result.chunks
                if chunk.text.strip()
            ),
            key=lambda item: (-item.score, item.chunk.chunk_index),
        )[:self._max_passages]
        return [
            self._adopt_excerpt(candidate, result, excerpt)
            for excerpt in excerpts
        ]

    def mark_failure(
        self,
        candidate: Evidence,
        result: DocumentReadResult,
    ) -> Evidence:
        updated = candidate.model_copy(deep=True)
        code = result.diagnostics.failure_code or "DOCUMENT_READ_UNAVAILABLE"
        warnings = list(updated.diagnostics.warnings)
        warnings.extend(result.diagnostics.warnings)
        if code not in warnings:
            warnings.append(code)
        updated.diagnostics = updated.diagnostics.model_copy(update={
            "warnings": list(dict.fromkeys(warnings)),
            "partial": True,
            "failure_code": code,
        })
        updated.access = updated.access.model_copy(update={
            "pdf_status": (
                result.status
                if updated.type == "academic" else updated.access.pdf_status
            ),
            "original_status": result.status,
            "fulltext": self._fulltext_state(updated, result),
        })
        return updated

    @staticmethod
    def _fulltext_state(
        candidate: Evidence,
        result: DocumentReadResult,
    ) -> EvidenceFulltext:
        integrity = result.integrity
        return EvidenceFulltext(
            status=result.status,
            source_url=(
                result.version.canonical_uri
                if result.version is not None
                else candidate.access.fulltext.source_url
            ),
            expected_pages=integrity.expected_pages,
            observed_pages=integrity.observed_pages,
            expected_chars=integrity.expected_chars,
            extracted_chars=integrity.extracted_chars,
            completeness_ratio=integrity.completeness_ratio,
            truncation_reasons=(
                list(integrity.truncation_reasons)
                or (
                    [result.diagnostics.failure_code]
                    if result.diagnostics.failure_code else []
                )
            ),
            attempts=result.diagnostics.attempts,
        )

    def _select_excerpt(
        self,
        chunk: DocumentChunk,
        claim_texts: Sequence[str],
    ) -> _Excerpt:
        text = chunk.text
        if len(text) <= self._max_excerpt_chars:
            return _Excerpt(
                chunk=chunk,
                start=0,
                end=len(text),
                score=self._score(text, claim_texts),
            )
        needles = list(dict.fromkeys(
            token
            for claim in claim_texts
            for token in text_tokens(claim)
            if len(token) >= 2
        ))
        starts = {0}
        searchable = text.casefold()
        for needle in needles:
            offset = searchable.find(needle.casefold())
            if offset >= 0:
                starts.add(max(0, offset - self._max_excerpt_chars // 3))
        candidates = []
        for start in starts:
            start = min(start, len(text) - self._max_excerpt_chars)
            end = min(len(text), start + self._max_excerpt_chars)
            candidates.append(_Excerpt(
                chunk=chunk,
                start=start,
                end=end,
                score=self._score(text[start:end], claim_texts),
            ))
        return max(candidates, key=lambda item: (item.score, -item.start))

    @staticmethod
    def _score(text: str, claim_texts: Sequence[str]) -> int:
        haystack = normalize_text(text)
        return sum(
            1
            for claim in claim_texts
            for token in text_tokens(claim)
            if normalize_text(token) in haystack
        )

    def _adopt_excerpt(
        self,
        candidate: Evidence,
        result: DocumentReadResult,
        excerpt: _Excerpt,
    ) -> Evidence:
        version = result.version
        assert version is not None
        chunk = excerpt.chunk
        text = chunk.text[excerpt.start:excerpt.end]
        locator = EvidenceLocator(
            document_id=chunk.locator.document_id,
            version_id=version.document_version_id,
            section=chunk.locator.section,
            subsection=chunk.locator.subsection,
            paragraph_id=chunk.locator.paragraph_id,
            page_from=chunk.locator.page_from,
            page_to=chunk.locator.page_to,
            char_start=excerpt.start,
            char_end=excerpt.end,
            table_id=chunk.locator.table_id,
            figure_id=chunk.locator.figure_id,
            claim_number=chunk.locator.claim_number,
            chunk_index=chunk.chunk_index,
        )
        stable_locator = bool(
            version.stable
            and version.storage_mode == "full_text"
            and locator.version_id
            and (
                locator.page_from is not None
                or locator.paragraph_id is not None
                or locator.claim_number is not None
            )
        )
        patent_claim_qualified = bool(
            candidate.type != "patent" or locator.claim_number
        )
        quality = EvidenceQuality(
            level="citable" if stable_locator else "limited",
            is_original=True,
            has_stable_locator=stable_locator,
            can_support_key_claim=stable_locator and patent_claim_qualified,
            reasons=(
                ["PATENT_SPECIFICATION_CONTEXT_ONLY"]
                if stable_locator and not patent_claim_qualified
                else []
                if stable_locator
                else [
                    "ORIGINAL_STORAGE_NOT_PERMITTED"
                    if version.storage_mode != "full_text"
                    else "DOCUMENT_VERSION_INCOMPLETE"
                ]
            ),
        )
        base_provenance = candidate.provenance or EvidenceProvenance(
            retrieved_at=version.retrieved_at
        )
        provenance = base_provenance.model_copy(deep=True, update={
            "canonical_url": version.canonical_uri,
            "content_origin": "fulltext",
            "document_id": version.source_record_id,
            "version_id": version.document_version_id,
            "source_record_id": version.source_record_id,
            "retrieved_at": version.retrieved_at,
            "parser_version": version.parser_version,
            "license": version.license or base_provenance.license,
            "syndication_group": (
                version.independent_work_id
                if candidate.type == "web"
                else base_provenance.syndication_group
            ),
        })
        warnings = list(candidate.diagnostics.warnings)
        warnings.extend(result.diagnostics.warnings)
        warnings.extend(quality.reasons)
        digest = hashlib.sha256(
            "|".join((
                version.document_version_id,
                str(chunk.chunk_index),
                str(excerpt.start),
                str(excerpt.end),
            )).encode("utf-8")
        ).hexdigest()[:16]
        snippet_type = {
            "academic": "pdf_text",
            "web": "web_original",
            "patent": (
                "patent_claim"
                if locator.claim_number else "patent_specification"
            ),
            "legal": "legal_text",
        }[candidate.type]
        patent = candidate.patent
        if patent is not None:
            relations = version.relations
            patent = patent.model_copy(update={
                "family_id": (
                    relations.get("family_id") or [patent.family_id]
                )[0],
                "priority_root": (
                    relations.get("priority_root") or [patent.priority_root]
                )[0],
                "priority_dates": relations.get(
                    "priority_dates", patent.priority_dates
                ),
                "application_date": (
                    relations.get("application_date")
                    or [patent.application_date]
                )[0],
                "publication_date": (
                    relations.get("publication_date")
                    or [patent.publication_date]
                )[0],
                "family_members": relations.get(
                    "family_members", patent.family_members
                ),
                "patent_citations": relations.get(
                    "patent_citations", patent.patent_citations
                ),
                "npl_citations": relations.get(
                    "npl_citations", patent.npl_citations
                ),
            })
        return candidate.model_copy(deep=True, update={
            "id": f"{candidate.result_id}:original:{digest}",
            "passage": EvidencePassage(
                text=text,
                snippet_type=snippet_type,
                char_start=excerpt.start,
                char_end=excerpt.end,
                page_from=locator.page_from,
                page_to=locator.page_to,
                chunk_index=locator.chunk_index,
            ),
            "access": candidate.access.model_copy(update={
                "pdf_status": (
                    result.status
                    if candidate.type == "academic"
                    else candidate.access.pdf_status
                ),
                "original_status": result.status,
                "next_cursor": result.next_cursor,
                "license": version.license or candidate.access.license,
                "fulltext": self._fulltext_state(candidate, result),
            }),
            "diagnostics": EvidenceDiagnostics(
                warnings=list(dict.fromkeys(warnings)),
                partial=result.status == "partial",
                failure_code=result.diagnostics.failure_code,
            ),
            "provenance": provenance,
            "locator": locator,
            "quality": quality,
            "patent": patent,
        })
