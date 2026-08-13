"""OpenAlex PDF enrichment tests."""
import os
import sys
from concurrent.futures import Future

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.infrastructure.openalex_pdf import OpenAlexPdfGateway
from src.models import AcademicResult


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "status": "ready",
            "pages": 3,
            "chunk_index": 0,
            "page_from": 1,
            "page_to": 2,
            "text_length": 1200,
            "text": "full text from pdf",
            "next_cursor": "cursor1",
            "error_code": None,
            "error_message": None,
        }


class _TextResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "work_id": "W123",
            "status": "ready",
            "chunk_index": 2,
            "page_from": 4,
            "page_to": 5,
            "text": "continued pdf text",
            "next_cursor": "cursor2",
            "error_code": None,
            "error_message": None,
        }


class _LegacyExtractResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "work_id": "W123",
            "status": "ready",
            "pages": 5,
            "text_length": 42,
            "text": "legacy transport text without a locator",
            "next_cursor": "legacy-cursor",
            "error_code": None,
            "error_message": None,
        }


class _FailedResp:
    def __init__(self, code, message):
        self.code = code
        self.message = message

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "work_id": "W123",
            "status": "failed",
            "pages": None,
            "text_length": 0,
            "text": None,
            "next_cursor": None,
            "error_code": self.code,
            "error_message": self.message,
        }


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def _gateway() -> OpenAlexPdfGateway:
    return OpenAlexPdfGateway(Settings(), requests, _InlineExecutor())


def test_pdf_enrichment_attaches_pdf_fields(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return _Resp()

    monkeypatch.setattr("src.infrastructure.openalex_pdf.requests.post", fake_post)
    gateway = _gateway()
    paper = AcademicResult(
        url="https://doi.org/10.1/example",
        title="Paper",
        content="abstract",
        work_id="W123",
        oa_pdf_url="https://example.org/paper.pdf",
    )

    outcome = gateway.enrich(
        [paper],
        include_pdf_text=True,
        pdf_text_mode="cached",
        pdf_max_results=1,
        pdf_max_chars_per_result=500,
        pdf_timeout_ms=3000,
    )
    enriched = outcome.academic[0]
    enriched_result = enriched.to_result()

    assert calls
    assert calls[0][1]["work_id"] == "W123"
    assert calls[0][1]["mode"] == "cached"
    assert paper.content == "abstract"
    assert paper.pdf_status == "not_requested"
    assert enriched_result.pdf_status == "ready"
    assert enriched_result.pdf_text == "full text from pdf"
    assert enriched_result.pdf_pages == 3
    assert enriched_result.pdf_chunk_index == 0
    assert enriched_result.pdf_page_from == 1
    assert enriched_result.pdf_page_to == 2
    assert enriched_result.pdf_next_cursor == "cursor1"


def test_pdf_enrichment_marks_missing_pdf_url():
    gateway = _gateway()
    paper = AcademicResult(
        url="https://openalex.org/W123",
        title="Paper",
        content="abstract",
        work_id="W123",
    )

    outcome = gateway.enrich(
        [paper],
        include_pdf_text=True,
        pdf_text_mode="sync",
        pdf_max_results=1,
        pdf_max_chars_per_result=500,
        pdf_timeout_ms=3000,
    )

    assert paper.pdf_status == "not_requested"
    enriched = outcome.academic[0].to_result()
    assert enriched.pdf_status == "no_pdf_url"
    assert enriched.pdf_error_code == "PDF_URL_MISSING"


def test_get_pdf_text_reads_next_page(monkeypatch):
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append((url, params, headers, timeout))
        return _TextResp()

    monkeypatch.setattr("src.infrastructure.openalex_pdf.requests.get", fake_get)
    gateway = _gateway()

    resp = gateway.read_page("W123", cursor="cursor1", max_chars=500)

    assert calls
    assert calls[0][0].endswith("/openalex/pdf/text/W123")
    assert calls[0][1] == {"max_chars": 500, "cursor": "cursor1"}
    assert resp.work_id == "W123"
    assert resp.status == "ready"
    assert resp.text == "continued pdf text"
    assert resp.returned_chars == len("continued pdf text")
    assert resp.next_cursor == "cursor2"
    assert resp.partial is True


def test_legacy_extract_recovers_first_page_locator_from_cached_text(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        return _LegacyExtractResp()

    def fake_get(url, params, headers, timeout):
        calls.append((url, params, headers, timeout))
        return _TextResp()

    monkeypatch.setattr("src.infrastructure.openalex_pdf.requests.post", fake_post)
    monkeypatch.setattr("src.infrastructure.openalex_pdf.requests.get", fake_get)
    paper = AcademicResult(
        url="https://doi.org/10.1/example",
        title="Paper",
        content="abstract",
        work_id="W123",
        oa_pdf_url="https://example.org/paper.pdf",
    )

    outcome = _gateway().enrich(
        [paper],
        include_pdf_text=True,
        pdf_text_mode="sync",
        pdf_max_results=1,
        pdf_max_chars_per_result=500,
        pdf_timeout_ms=3000,
    )
    enriched = outcome.academic[0].to_result()

    assert calls
    assert calls[0][1] == {"max_chars": 500}
    assert enriched.pdf_text == "continued pdf text"
    assert enriched.pdf_chunk_index == 2
    assert enriched.pdf_page_from == 4
    assert enriched.pdf_page_to == 5
    assert enriched.pdf_next_cursor == "cursor2"


def test_pdf_enrichment_classifies_permanent_http_failures(monkeypatch):
    responses = iter([
        _FailedResp("DOWNLOAD_FAILED", "HTTP 403"),
        _FailedResp("DOWNLOAD_FAILED", "HTTP 404"),
    ])

    def fake_post(url, json, headers, timeout):
        return next(responses)

    monkeypatch.setattr("src.infrastructure.openalex_pdf.requests.post", fake_post)
    papers = [
        AcademicResult(
            url=f"https://doi.org/10.1/{index}",
            title=f"Paper {index}",
            content="abstract",
            work_id=f"W{index}",
            oa_pdf_url=f"https://example.org/{index}.pdf",
        )
        for index in range(2)
    ]

    outcome = _gateway().enrich(
        papers,
        include_pdf_text=True,
        pdf_text_mode="sync",
        pdf_max_results=2,
        pdf_timeout_ms=3000,
    )

    assert [item.to_result().pdf_error_code for item in outcome.academic] == [
        "PDF_ACCESS_DENIED",
        "PDF_NOT_FOUND",
    ]
