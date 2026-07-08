"""OpenAlex PDF enrichment tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.engine import SearchEngine
from src.models import AcademicResult


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "status": "ready",
            "pages": 3,
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


def test_pdf_enrichment_attaches_pdf_fields(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return _Resp()

    monkeypatch.setattr("src.engine.requests.post", fake_post)
    engine = object.__new__(SearchEngine)
    paper = AcademicResult(
        url="https://doi.org/10.1/example",
        title="Paper",
        content="abstract",
        work_id="W123",
        oa_pdf_url="https://example.org/paper.pdf",
    )

    engine._enrich_academic_pdf_text(
        [paper],
        include_pdf_text=True,
        pdf_text_mode="cached",
        pdf_max_results=1,
        pdf_max_chars_per_result=500,
        pdf_timeout_ms=3000,
    )

    assert calls
    assert calls[0][1]["work_id"] == "W123"
    assert calls[0][1]["mode"] == "cached"
    assert paper.content == "abstract"
    assert paper.pdf_status == "ready"
    assert paper.pdf_text == "full text from pdf"
    assert paper.pdf_pages == 3
    assert paper.pdf_next_cursor == "cursor1"


def test_pdf_enrichment_marks_missing_pdf_url():
    engine = object.__new__(SearchEngine)
    paper = AcademicResult(
        url="https://openalex.org/W123",
        title="Paper",
        content="abstract",
        work_id="W123",
    )

    engine._enrich_academic_pdf_text(
        [paper],
        include_pdf_text=True,
        pdf_text_mode="sync",
        pdf_max_results=1,
        pdf_max_chars_per_result=500,
        pdf_timeout_ms=3000,
    )

    assert paper.pdf_status == "no_pdf_url"
    assert paper.pdf_error_code == "PDF_URL_MISSING"


def test_get_pdf_text_reads_next_page(monkeypatch):
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append((url, params, headers, timeout))
        return _TextResp()

    monkeypatch.setattr("src.engine.requests.get", fake_get)
    engine = object.__new__(SearchEngine)

    resp = engine.get_pdf_text("W123", cursor="cursor1", max_chars=500)

    assert calls
    assert calls[0][0].endswith("/openalex/pdf/text/W123")
    assert calls[0][1] == {"max_chars": 500, "cursor": "cursor1"}
    assert resp.work_id == "W123"
    assert resp.status == "ready"
    assert resp.text == "continued pdf text"
    assert resp.returned_chars == len("continued pdf text")
    assert resp.next_cursor == "cursor2"
    assert resp.partial is True
