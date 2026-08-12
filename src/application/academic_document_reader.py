"""Bounded Academic PDF reader built on the existing OpenAlex gateway."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Callable

from src.application.document_identity import (
    academic_document_version_id,
    academic_independent_work_id,
)
from src.application.ports.pdf_text import PdfTextGateway
from src.application.research_execution import ExecutionContext
from src.domain.document_read import (
    DocumentChunk,
    DocumentReadDiagnostics,
    DocumentReadIntegrity,
    DocumentReadResult,
    DocumentVersion,
)
from src.domain.documents import RankedDocument, RetrievedDocument
from src.domain.evidence import Evidence, EvidenceLocator
from src.domain.pdf_text import PdfTextPage
from src.domain.search import AcademicResult


_ADAPTER_PARSER_VERSION = "openalex-pdf-text.v1"
_NON_RETRYABLE_INITIAL_CODES = {
    "WORK_ID_MISSING",
    "PDF_URL_MISSING",
    "ACADEMIC_PDF_LICENSE_UNVERIFIED",
    "PDF_TOTAL_BUDGET_EXCEEDED",
}


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class AcademicDocumentReader:
    def __init__(
        self,
        gateway: PdfTextGateway,
        *,
        max_chunks: int = 20,
        max_chars_per_chunk: int = 30_000,
        max_total_bytes: int = 600_000,
        max_initial_attempts: int = 2,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._max_chunks = max(1, max_chunks)
        self._max_chars_per_chunk = max(1, min(max_chars_per_chunk, 30_000))
        self._max_total_bytes = max(1, max_total_bytes)
        self._max_initial_attempts = max(1, min(max_initial_attempts, 3))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def read(
        self,
        candidate: Evidence,
        *,
        context: ExecutionContext,
    ) -> DocumentReadResult:
        if candidate.type != "academic":
            return self._failure("DOCUMENT_TYPE_UNSUPPORTED", retryable=False)
        work_id = (candidate.citation.work_id or "").strip()
        pdf_url = (candidate.access.oa_pdf_url or "").strip()
        if not work_id:
            return self._failure("WORK_ID_MISSING", retryable=False)
        if not pdf_url:
            return self._failure("PDF_URL_MISSING", retryable=False)
        if not candidate.access.is_open and not candidate.access.license:
            return self._failure(
                "ACADEMIC_PDF_LICENSE_UNVERIFIED",
                retryable=False,
            )

        ranked = self._ranked_candidate(candidate, work_id, pdf_url)
        initial_attempts = 0
        initial_retried = False
        initial_warnings: list[str] = []
        paper: AcademicResult | None = None
        last_code = "PDF_TEXT_UNAVAILABLE"
        last_retryable = True
        while initial_attempts < self._max_initial_attempts:
            context.checkpoint()
            initial_attempts += 1
            outcome = self._gateway.enrich(
                [ranked],
                include_pdf_text=True,
                pdf_text_mode="sync",
                pdf_max_results=1,
                pdf_max_chars_per_result=self._max_chars_per_chunk,
                deadline=context.deadline,
            )
            if outcome.academic:
                materialized = outcome.academic[0].to_result()
                assert isinstance(materialized, AcademicResult)
                if materialized.pdf_text:
                    paper = materialized
                    break
                last_code = (
                    materialized.pdf_error_code or "PDF_TEXT_UNAVAILABLE"
                )
            else:
                last_code = (
                    outcome.failures[0].code
                    if outcome.failures else "PDF_TEXT_UNAVAILABLE"
                )
            last_retryable = last_code not in _NON_RETRYABLE_INITIAL_CODES
            if (
                not last_retryable
                or initial_attempts >= self._max_initial_attempts
                or context.deadline.expired
            ):
                break
            initial_retried = True
            initial_warnings.append("PDF_INITIAL_READ_RETRY")
        if paper is None:
            return self._failure(
                last_code,
                retryable=last_retryable,
                warnings=initial_warnings,
                attempts=initial_attempts,
                retried=initial_retried,
            )

        pages: list[PdfTextPage] = [PdfTextPage(
            work_id=work_id,
            status=paper.pdf_status or "ready",
            chunk_index=paper.pdf_chunk_index,
            page_from=paper.pdf_page_from,
            page_to=paper.pdf_page_to,
            text=paper.pdf_text,
            returned_chars=len(paper.pdf_text),
            next_cursor=paper.pdf_next_cursor,
            partial=bool(paper.pdf_next_cursor),
            content_hash=paper.pdf_content_hash,
            parser_version=paper.pdf_parser_version,
            source_version_id=paper.pdf_version_id,
        )]
        cursor = paper.pdf_next_cursor
        seen_cursors: set[str] = set()
        warnings: list[str] = list(initial_warnings)
        failure_code: str | None = None
        retryable = True
        total_bytes = len(paper.pdf_text.encode("utf-8"))

        while cursor and len(pages) < self._max_chunks:
            context.checkpoint()
            if cursor in seen_cursors:
                failure_code = "PDF_CURSOR_LOOP"
                retryable = False
                break
            seen_cursors.add(cursor)
            page = self._gateway.read_page(
                work_id,
                cursor=cursor,
                max_chars=self._max_chars_per_chunk,
                deadline=context.deadline,
            )
            if page.status != "ready" or not page.text:
                failure_code = page.error_code or "PDF_TEXT_READ_FAILED"
                retryable = failure_code not in {
                    "WORK_ID_MISSING", "PDF_CURSOR_INVALID"
                }
                break
            page_bytes = len(page.text.encode("utf-8"))
            if total_bytes + page_bytes > self._max_total_bytes:
                failure_code = "PDF_BYTE_LIMIT_EXCEEDED"
                retryable = False
                break
            pages.append(page)
            total_bytes += page_bytes
            cursor = page.next_cursor

        if cursor and len(pages) >= self._max_chunks and failure_code is None:
            failure_code = "PDF_CHUNK_LIMIT_EXCEEDED"
            retryable = False
        complete = cursor is None and failure_code is None
        upstream_hashes = {
            page.content_hash for page in pages if page.content_hash
        }
        if len(upstream_hashes) > 1:
            warnings.append("PDF_CONTENT_HASH_CHANGED_DURING_READ")
            complete = False
            failure_code = failure_code or "PDF_VERSION_CHANGED_DURING_READ"
        combined_text = "\x1e".join(page.text or "" for page in pages)
        extracted_chars = sum(len(page.text or "") for page in pages)
        observed_pages = self._observed_page_count(pages)
        integrity_reasons: list[str] = []
        expected_pages = paper.pdf_pages if paper.pdf_pages else None
        expected_chars = paper.pdf_text_length or None
        if expected_pages is not None and observed_pages > 0:
            if observed_pages < expected_pages:
                integrity_reasons.append("PDF_PAGE_COUNT_MISMATCH")
            elif observed_pages > expected_pages:
                warnings.append("PDF_PAGE_COUNT_EXCEEDS_DECLARED")
        elif expected_pages is not None and pages:
            warnings.append("PDF_PAGE_RANGE_UNAVAILABLE")
        if expected_chars is not None:
            if extracted_chars < expected_chars:
                integrity_reasons.append("PDF_TEXT_TRUNCATED")
            elif extracted_chars > expected_chars:
                warnings.append("PDF_TEXT_LENGTH_EXCEEDS_DECLARED")
        if failure_code:
            integrity_reasons.append(failure_code)
        if integrity_reasons:
            complete = False
            failure_code = failure_code or integrity_reasons[0]
        integrity = DocumentReadIntegrity(
            expected_pages=expected_pages,
            observed_pages=observed_pages,
            expected_chars=expected_chars,
            extracted_chars=extracted_chars,
            truncation_reasons=list(dict.fromkeys(integrity_reasons)),
        )
        content_hash = (
            next(iter(upstream_hashes))
            if len(upstream_hashes) == 1
            else _sha256_text(combined_text)
        )
        hash_scope = (
            "full_document"
            if complete and len(upstream_hashes) == 1
            else ("extracted_text" if complete else "observed_chunks")
        )
        parser_versions = {
            page.parser_version for page in pages if page.parser_version
        }
        if len(parser_versions) > 1:
            warnings.append("PDF_PARSER_VERSION_CHANGED_DURING_READ")
        parser_version = (
            sorted(parser_versions)[0]
            if parser_versions else _ADAPTER_PARSER_VERSION
        )
        source_versions = {
            page.source_version_id
            for page in pages
            if page.source_version_id
        }
        if len(source_versions) > 1:
            warnings.append("PDF_SOURCE_VERSION_CHANGED_DURING_READ")
        version_id = academic_document_version_id(candidate, content_hash)
        retrieved_at = self._now()
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
        version = DocumentVersion(
            document_version_id=version_id,
            independent_work_id=academic_independent_work_id(candidate),
            type="academic",
            source_record_id=work_id,
            source_version_id=(
                sorted(source_versions)[0] if source_versions else None
            ),
            canonical_uri=pdf_url,
            content_hash=content_hash,
            content_hash_scope=hash_scope,
            parser_version=parser_version,
            retrieved_at=retrieved_at.astimezone(timezone.utc).isoformat(),
            complete=complete,
            license=candidate.access.license,
        )
        chunks: list[DocumentChunk] = []
        used_indexes: set[int] = set()
        for fallback_index, page in enumerate(pages):
            chunk_index = (
                page.chunk_index
                if page.chunk_index is not None else fallback_index
            )
            while chunk_index in used_indexes:
                chunk_index += 1
            used_indexes.add(chunk_index)
            chunks.append(self._chunk(
                version,
                page,
                chunk_index=chunk_index,
            ))
        if not version.stable:
            warnings.append("DOCUMENT_VERSION_INCOMPLETE")
        if failure_code:
            warnings.append(failure_code)
        return DocumentReadResult(
            status="ready" if complete else "partial",
            version=version,
            chunks=chunks,
            next_cursor=cursor,
            diagnostics=DocumentReadDiagnostics(
                warnings=list(dict.fromkeys(warnings)),
                failure_code=failure_code,
                message=(
                    "Academic PDF read completed."
                    if complete else "Academic PDF read is partial."
                ),
                retryable=retryable,
                attempts=initial_attempts,
                retried=initial_retried,
            ),
            integrity=integrity,
            pages_read=self._page_count(pages),
            bytes_read=total_bytes,
        )

    @staticmethod
    def _ranked_candidate(
        candidate: Evidence,
        work_id: str,
        pdf_url: str,
    ) -> RankedDocument:
        paper = AcademicResult(
            url=candidate.url,
            title=candidate.title,
            content=candidate.passage.text,
            date=candidate.published_date,
            site=candidate.citation.venue,
            source=candidate.source,
            rerank_score=candidate.scores.rerank_score,
            authors=candidate.citation.authors,
            work_id=work_id,
            year=candidate.citation.year,
            venue=candidate.citation.venue,
            doi=candidate.citation.doi or "",
            oa_pdf_url=pdf_url,
            license=candidate.access.license or "",
            is_oa=candidate.access.is_open,
        )
        return RankedDocument(
            document=RetrievedDocument.from_result(paper, "academic"),
            score=candidate.scores.rerank_score,
            ranking_profile="research-deep-read",
        )

    @staticmethod
    def _chunk(
        version: DocumentVersion,
        page: PdfTextPage,
        *,
        chunk_index: int,
    ) -> DocumentChunk:
        text = page.text or ""
        return DocumentChunk(
            document_version_id=version.document_version_id,
            chunk_index=chunk_index,
            text=text,
            text_hash=_sha256_text(text),
            locator=EvidenceLocator(
                document_id=version.source_record_id,
                version_id=version.document_version_id,
                page_from=page.page_from,
                page_to=page.page_to,
                char_start=0,
                char_end=len(text),
                chunk_index=chunk_index,
            ),
        )

    @staticmethod
    def _page_count(pages: list[PdfTextPage]) -> int:
        observed: set[int] = set()
        for page in pages:
            if page.page_from is None:
                continue
            page_to = page.page_to if page.page_to is not None else page.page_from
            observed.update(range(page.page_from, page_to + 1))
        return len(observed) or len(pages)

    @staticmethod
    def _observed_page_count(pages: list[PdfTextPage]) -> int:
        observed: set[int] = set()
        for page in pages:
            if page.page_from is None:
                continue
            page_to = page.page_to if page.page_to is not None else page.page_from
            observed.update(range(page.page_from, page_to + 1))
        return len(observed)

    @staticmethod
    def _failure(
        code: str,
        *,
        retryable: bool,
        warnings: list[str] | None = None,
        attempts: int = 1,
        retried: bool = False,
    ) -> DocumentReadResult:
        return DocumentReadResult(
            status="unavailable" if not retryable else "failed",
            diagnostics=DocumentReadDiagnostics(
                warnings=list(dict.fromkeys([*(warnings or ()), code])),
                failure_code=code,
                message="Academic PDF text is unavailable.",
                retryable=retryable,
                attempts=attempts,
                retried=retried,
            ),
        )
