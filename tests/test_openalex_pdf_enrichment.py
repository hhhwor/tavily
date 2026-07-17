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

    assert calls
    assert calls[0][1]["work_id"] == "W123"
    assert calls[0][1]["mode"] == "cached"
    assert paper.content == "abstract"
    assert paper.pdf_status == "not_requested"
    assert enriched.pdf_status == "ready"
    assert enriched.pdf_text == "full text from pdf"
    assert enriched.pdf_pages == 3
    assert enriched.pdf_chunk_index == 0
    assert enriched.pdf_page_from == 1
    assert enriched.pdf_page_to == 2
    assert enriched.pdf_next_cursor == "cursor1"


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
    assert outcome.academic[0].pdf_status == "no_pdf_url"
    assert outcome.academic[0].pdf_error_code == "PDF_URL_MISSING"


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
