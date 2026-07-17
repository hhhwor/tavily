"""OpenAlex PDF gateway 的 HTTP、预算与错误契约。"""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings
from src.infrastructure.openalex_pdf import OpenAlexPdfGateway
from src.models import AcademicResult


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Http:
    def __init__(self, *, post_result=None, get_result=None):
        self.post_result = post_result
        self.get_result = get_result
        self.post_calls = []
        self.get_calls = []

    def post(self, url, *, json, headers, timeout):
        self.post_calls.append((url, json, headers, timeout))
        if isinstance(self.post_result, BaseException):
            raise self.post_result
        return _Response(self.post_result)

    def get(self, url, *, params, headers, timeout):
        self.get_calls.append((url, params, headers, timeout))
        if isinstance(self.get_result, BaseException):
            raise self.get_result
        return _Response(self.get_result)


def _settings(**overrides) -> Settings:
    values = {
        "openalex_api_url": "https://openalex.internal",
        "openalex_api_key": "test-key",
        "openalex_pdf_text_mode": "sync",
        "openalex_pdf_max_results": 2,
        "openalex_pdf_max_chars": 8000,
        "openalex_pdf_timeout_ms": 10000,
        "openalex_pdf_total_budget_ms": 15000,
        "provider_timeout": 7,
    }
    values.update(overrides)
    return Settings(**values)


def _gateway(http: _Http, *, settings=None, monotonic=lambda: 0.0):
    return OpenAlexPdfGateway(
        settings or _settings(),
        http,
        _InlineExecutor(),
        monotonic=monotonic,
    )


def _paper(**overrides) -> AcademicResult:
    values = {
        "url": "https://doi.org/10.1/example",
        "title": "Paper",
        "content": "abstract",
        "work_id": "W123",
        "doi": "10.1/example",
        "oa_pdf_url": "https://example.org/paper.pdf",
    }
    values.update(overrides)
    return AcademicResult(**values)


def test_enrich_returns_copies_and_preserves_http_contract():
    http = _Http(post_result={
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
    })
    original = _paper()

    outcome = _gateway(http).enrich(
        [original],
        include_pdf_text=True,
        pdf_text_mode="cached",
        pdf_max_results=1,
        pdf_max_chars_per_result=500,
        pdf_timeout_ms=3000,
    )

    assert outcome.failures == ()
    assert len(outcome.academic) == 1
    enriched = outcome.academic[0]
    assert enriched is not original
    assert original.pdf_status == "not_requested"
    assert original.pdf_text == ""
    assert enriched.content == "abstract"
    assert enriched.pdf_status == "ready"
    assert enriched.pdf_text == "full text from pdf"
    assert enriched.pdf_pages == 3
    assert enriched.pdf_chunk_index == 0
    assert enriched.pdf_page_from == 1
    assert enriched.pdf_page_to == 2
    assert enriched.pdf_next_cursor == "cursor1"

    assert len(http.post_calls) == 1
    url, body, headers, timeout = http.post_calls[0]
    assert url == "https://openalex.internal/openalex/pdf/extract"
    assert body == {
        "work_id": "W123",
        "mode": "cached",
        "max_chars": 500,
        "timeout_ms": 3000,
    }
    assert headers == {
        "Content-Type": "application/json",
        "X-API-Key": "test-key",
    }
    assert timeout == 5.0


def test_enrich_reports_missing_identity_and_pdf_url_without_http():
    http = _Http()
    papers = [
        _paper(work_id="", title="Missing work"),
        _paper(work_id="W-no-pdf", oa_pdf_url="", title="Missing PDF"),
    ]

    outcome = _gateway(http).enrich(papers, include_pdf_text=True)

    assert http.post_calls == []
    assert [paper.pdf_status for paper in outcome.academic] == [
        "failed",
        "no_pdf_url",
    ]
    assert [failure.code for failure in outcome.failures] == [
        "WORK_ID_MISSING",
        "PDF_URL_MISSING",
    ]
    assert all(paper.pdf_status == "not_requested" for paper in papers)


def test_enrich_maps_download_timeout_to_structured_failure():
    http = _Http(post_result=requests.Timeout("slow"))

    outcome = _gateway(http).enrich([_paper()], include_pdf_text=True)

    assert outcome.academic[0].pdf_status == "timeout"
    assert outcome.academic[0].pdf_error_code == "DOWNLOAD_TIMEOUT"
    assert outcome.failures[0].stage == "pdf_enrichment"
    assert outcome.failures[0].source == "W123"
    assert outcome.failures[0].type == "academic"
    assert outcome.failures[0].code == "DOWNLOAD_TIMEOUT"


def test_enrich_stops_before_http_when_total_budget_is_exhausted():
    clock = iter([0.0, 16.0])
    http = _Http(post_result={})

    outcome = _gateway(http, monotonic=lambda: next(clock)).enrich(
        [_paper()], include_pdf_text=True
    )

    assert http.post_calls == []
    assert outcome.academic[0].pdf_status == "timeout"
    assert outcome.failures[0].code == "PDF_TOTAL_BUDGET_EXCEEDED"


def test_enrich_maps_post_processing_errors_to_worker_failure():
    http = _Http(post_result={
        "status": "ready",
        "text": "text",
        "text_length": "not-an-integer",
    })

    outcome = _gateway(http).enrich([_paper()], include_pdf_text=True)

    assert outcome.academic[0].pdf_status == "failed"
    assert outcome.academic[0].pdf_error_code == "PDF_ENRICH_WORKER_FAILED"
    assert outcome.failures[0].code == "PDF_ENRICH_WORKER_FAILED"
    assert "invalid literal" in outcome.failures[0].message


def test_enrich_disabled_returns_explicit_copies_without_failures():
    http = _Http()
    original = _paper()

    outcome = _gateway(http).enrich([original], include_pdf_text=False)

    assert outcome.failures == ()
    assert outcome.academic[0] is not original
    assert outcome.academic[0] == original
    assert http.post_calls == []


def test_read_page_reads_cached_text_and_encodes_work_id():
    http = _Http(get_result={
        "work_id": "W/123",
        "status": "ready",
        "chunk_index": 2,
        "page_from": 4,
        "page_to": 5,
        "text": "continued pdf text",
        "next_cursor": "cursor2",
        "error_code": None,
        "error_message": None,
    })

    response = _gateway(http).read_page("W/123", "cursor1", 500)

    assert response.work_id == "W/123"
    assert response.status == "ready"
    assert response.text == "continued pdf text"
    assert response.returned_chars == len("continued pdf text")
    assert response.next_cursor == "cursor2"
    assert response.partial is True
    assert http.get_calls == [(
        "https://openalex.internal/openalex/pdf/text/W%2F123",
        {"max_chars": 500, "cursor": "cursor1"},
        {"X-API-Key": "test-key"},
        7,
    )]


def test_read_page_validates_work_id_and_maps_timeout():
    unused_http = _Http()
    missing = _gateway(unused_http).read_page("   ")
    assert missing.error_code == "WORK_ID_MISSING"
    assert unused_http.get_calls == []

    timeout_http = _Http(get_result=requests.Timeout("slow"))
    timed_out = _gateway(timeout_http).read_page("W123")
    assert timed_out.status == "failed"
    assert timed_out.error_code == "PDF_TEXT_TIMEOUT"
