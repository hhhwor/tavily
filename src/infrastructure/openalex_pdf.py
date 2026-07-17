"""OpenAlex PDF 服务适配器。"""
from __future__ import annotations

import re
import time
from concurrent.futures import Executor, as_completed
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote

import requests

from src.application.outcomes import PdfEnrichmentOutcome
from src.application.ports.pdf_text import PdfTextGateway
from src.config import Settings
from src.domain.documents import EnrichedDocument, RankedDocument, RetrievedDocument
from src.domain.errors import public_error_message
from src.models import AcademicResult, PdfTextResponse, SearchFailure


_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _stable_error_code(value: object, fallback: str) -> str:
    code = str(value or "")
    return code if _ERROR_CODE.fullmatch(code) else fallback


def _external_error_message(code: str) -> str:
    return f"openalex_pdf external service failed ({code})"


class OpenAlexPdfGateway(PdfTextGateway):
    """通过 OpenAlex 辅助服务富化和分页读取 PDF 正文。"""

    def __init__(
        self,
        settings: Settings,
        http_session: Any,
        executor: Executor,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http_session
        self._executor = executor
        self._monotonic = monotonic

    @staticmethod
    def _failure(paper: AcademicResult) -> SearchFailure:
        return SearchFailure(
            stage="pdf_enrichment",
            source=paper.work_id or paper.doi or paper.title,
            type="academic",
            code=paper.pdf_error_code or "PDF_ENRICH_FAILED",
            message=public_error_message(paper.pdf_error_message or paper.pdf_status),
            recoverable=True,
        )

    def enrich(
        self,
        papers: Sequence[RankedDocument],
        *,
        include_pdf_text: bool,
        pdf_text_mode: Optional[str] = None,
        pdf_max_results: Optional[int] = None,
        pdf_max_chars_per_result: Optional[int] = None,
        pdf_timeout_ms: Optional[int] = None,
    ) -> PdfEnrichmentOutcome:
        """在临时适配器 DTO 上执行 I/O，返回不可变 EnrichedDocument。"""
        ranked = tuple(
            document
            if isinstance(document, RankedDocument)
            else RankedDocument(
                document=RetrievedDocument.from_result(document, "academic"),
                score=document.rerank_score,
                ranking_profile="quality",
            )
            for document in papers
        )
        materialized: list[AcademicResult] = []
        for document in ranked:
            result = document.to_result()
            if not isinstance(result, AcademicResult):
                raise TypeError("OpenAlexPdfGateway 只接受 academic RankedDocument")
            materialized.append(result)

        def outcome(failures=()) -> PdfEnrichmentOutcome:
            return PdfEnrichmentOutcome(
                academic=tuple(
                    EnrichedDocument.from_result(document, result)
                    for document, result in zip(ranked, materialized)
                ),
                failures=tuple(failures),
            )

        if not include_pdf_text or not ranked:
            return outcome()

        mode = (
            pdf_text_mode or self._settings.openalex_pdf_text_mode or "sync"
        ).strip().lower()
        if mode not in {"cached", "sync"}:
            mode = "sync"

        max_results = (
            self._settings.openalex_pdf_max_results
            if pdf_max_results is None
            else pdf_max_results
        )
        max_results = max(0, min(max_results, 5))
        max_chars = (
            self._settings.openalex_pdf_max_chars
            if pdf_max_chars_per_result is None
            else pdf_max_chars_per_result
        )
        max_chars = max(1, min(max_chars, 30000))
        timeout_ms = (
            self._settings.openalex_pdf_timeout_ms
            if pdf_timeout_ms is None
            else pdf_timeout_ms
        )
        timeout_ms = max(1000, min(timeout_ms, 60000))
        if max_results <= 0:
            return outcome()

        candidates: list[AcademicResult] = []
        for paper in materialized:
            if not paper.work_id:
                paper.pdf_status = "failed"
                paper.pdf_error_code = "WORK_ID_MISSING"
                continue
            if not paper.oa_pdf_url:
                paper.pdf_status = "no_pdf_url"
                paper.pdf_error_code = "PDF_URL_MISSING"
                continue
            candidates.append(paper)
            if len(candidates) >= max_results:
                break

        headers = {"Content-Type": "application/json"}
        if self._settings.openalex_api_key:
            headers["X-API-Key"] = self._settings.openalex_api_key
        endpoint = (
            f"{self._settings.openalex_api_url.rstrip('/')}/openalex/pdf/extract"
        )
        deadline = self._monotonic() + (
            self._settings.openalex_pdf_total_budget_ms / 1000
        )

        def enrich_one(paper: AcademicResult) -> None:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                paper.pdf_status = "timeout"
                paper.pdf_error_code = "PDF_TOTAL_BUDGET_EXCEEDED"
                return
            budget_ms = min(timeout_ms, int(remaining * 1000))
            try:
                response = self._http.post(
                    endpoint,
                    json={
                        "work_id": paper.work_id,
                        "mode": mode,
                        "max_chars": max_chars,
                        "timeout_ms": budget_ms,
                    },
                    headers=headers,
                    timeout=max(1, budget_ms / 1000 + 2),
                )
                response.raise_for_status()
                data = response.json()
            except requests.Timeout:
                paper.pdf_status = "timeout"
                paper.pdf_error_code = "DOWNLOAD_TIMEOUT"
                return
            except Exception as exc:
                paper.pdf_status = "failed"
                paper.pdf_error_code = "PDF_ENRICH_FAILED"
                paper.pdf_error_message = public_error_message(exc, limit=300)
                return

            paper.pdf_status = data.get("status") or "failed"
            paper.pdf_text = data.get("text") or ""
            paper.pdf_pages = data.get("pages")
            paper.pdf_text_length = int(data.get("text_length") or 0)
            paper.pdf_returned_chars = len(paper.pdf_text)
            paper.pdf_chunk_index = data.get("chunk_index")
            paper.pdf_page_from = data.get("page_from")
            paper.pdf_page_to = data.get("page_to")
            paper.pdf_next_cursor = data.get("next_cursor")
            paper.pdf_error_code = (
                _stable_error_code(data.get("error_code"), "PDF_ENRICH_FAILED")
                if data.get("error_code")
                else None
            )
            paper.pdf_error_message = (
                _external_error_message(paper.pdf_error_code)
                if paper.pdf_error_code
                else None
            )

        futures = {
            self._executor.submit(enrich_one, paper): paper for paper in candidates
        }
        for future in as_completed(futures):
            paper = futures[future]
            try:
                future.result()
            except Exception as exc:
                paper.pdf_status = "failed"
                paper.pdf_error_code = "PDF_ENRICH_WORKER_FAILED"
                paper.pdf_error_message = public_error_message(exc, limit=300)

        failures = tuple(
            self._failure(paper) for paper in materialized if paper.pdf_error_code
        )
        return outcome(failures)

    def read_page(
        self,
        work_id: str,
        cursor: Optional[str] = None,
        max_chars: Optional[int] = None,
    ) -> PdfTextResponse:
        """读取已缓存的 PDF 正文分页。"""
        work_id = (work_id or "").strip()
        if not work_id:
            return PdfTextResponse(
                work_id="",
                status="failed",
                error_code="WORK_ID_MISSING",
                error_message="work_id is required",
            )

        chars = (
            self._settings.openalex_pdf_max_chars
            if max_chars is None
            else max_chars
        )
        chars = max(1, min(int(chars), 30000))
        endpoint = (
            f"{self._settings.openalex_api_url.rstrip('/')}/openalex/pdf/text/"
            f"{quote(work_id, safe='')}"
        )
        params: dict[str, object] = {"max_chars": chars}
        if cursor:
            params["cursor"] = cursor
        headers = {}
        if self._settings.openalex_api_key:
            headers["X-API-Key"] = self._settings.openalex_api_key
        try:
            response = self._http.get(
                endpoint,
                params=params,
                headers=headers or None,
                timeout=max(1, self._settings.provider_timeout),
            )
            response.raise_for_status()
            data = response.json()
        except requests.Timeout:
            return PdfTextResponse(
                work_id=work_id,
                status="failed",
                error_code="PDF_TEXT_TIMEOUT",
                error_message="PDF text read timed out",
            )
        except Exception as exc:
            return PdfTextResponse(
                work_id=work_id,
                status="failed",
                error_code="PDF_TEXT_READ_FAILED",
                error_message=public_error_message(exc, limit=300),
            )

        text = data.get("text")
        error_code = (
            _stable_error_code(data.get("error_code"), "PDF_TEXT_READ_FAILED")
            if data.get("error_code")
            else None
        )
        return PdfTextResponse(
            work_id=data.get("work_id") or work_id,
            status=data.get("status") or "failed",
            chunk_index=data.get("chunk_index"),
            page_from=data.get("page_from"),
            page_to=data.get("page_to"),
            text=text,
            returned_chars=len(text or ""),
            next_cursor=data.get("next_cursor"),
            partial=bool(data.get("next_cursor")),
            error_code=error_code,
            error_message=_external_error_message(error_code) if error_code else None,
        )
