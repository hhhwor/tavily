from datetime import datetime, timezone

import pytest

from src.application.ports.retrieval import RetrievalRequest
from src.domain.errors import ExternalServiceError
from src.infrastructure.doubao_mcp import DoubaoResponseError
from src.providers.doubao import DoubaoSearchProvider


class _Client:
    def __init__(self, rows=None, error: BaseException | None = None):
        self.rows = rows or []
        self.error = error
        self.calls = []
        self.closed = False

    def search(self, query, *, count, time_range=None, timeout):
        self.calls.append({
            "query": query,
            "count": count,
            "time_range": time_range,
            "timeout": timeout,
        })
        if self.error is not None:
            raise self.error
        return self.rows

    def close(self):
        self.closed = True


def test_doubao_retrieval_reports_actual_boundary_and_normalizes_results():
    row = {
        "Title": "Example",
        "Url": "https://example.test/result",
        "Summary": "Query-specific summary.",
        "Snippet": "Provider snippet.",
        "Content": "Full page content.",
        "SiteName": "Example",
        "PublishTime": "2026-07-29T00:00:00Z",
        "RankScore": "0.91",
        "AuthInfoLevel": 2,
    }
    client = _Client([row])
    provider = DoubaoSearchProvider(
        api_key="test-key",
        timeout=15,
        client=client,
    )
    request = RetrievalRequest(
        query="long query " * 20,
        candidate_budget=80,
        time_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        time_to=datetime(2026, 7, 29, tzinfo=timezone.utc),
        timeout_seconds=4.5,
    )

    batch = provider.retrieve(request)

    assert len(batch.actual_query) <= 100
    assert batch.actual_filters.to_dict() == {
        "Count": 50,
        "SearchType": "web",
        "TimeRange": "2026-07-01..2026-07-29",
    }
    assert batch.diagnostics.to_dict()["applied_request_filters"] == {
        "published_from": "2026-07-01",
        "published_to": "2026-07-29",
    }
    assert client.calls == [{
        "query": batch.actual_query,
        "count": 50,
        "time_range": "2026-07-01..2026-07-29",
        "timeout": 4.5,
    }]
    document = batch.documents[0]
    assert document.source == "doubao"
    assert document.title == "Example"
    assert document.snippet == "Query-specific summary."
    assert document.content == "Full page content."
    assert document.source_score == pytest.approx(0.91)
    assert "Content" not in document.raw_payload
    assert batch.source.default_snapshot.startswith("mcp-server:")


def test_doubao_search_maps_recency_and_closes_persistent_client():
    client = _Client()
    provider = DoubaoSearchProvider(
        api_key="test-key",
        timeout=7,
        client=client,
    )

    assert provider.search("query", top_k=3, recency="month") == []
    assert client.calls[0] == {
        "query": "query",
        "count": 3,
        "time_range": "OneMonth",
        "timeout": 7.0,
    }

    provider.close()
    assert client.closed is True


def test_doubao_provider_uses_stable_failure_contract():
    provider = DoubaoSearchProvider(
        api_key="test-key",
        client=_Client(error=TimeoutError("secret upstream detail")),
    )

    with pytest.raises(ExternalServiceError) as caught:
        provider.search("query")

    assert caught.value.provider == "doubao"
    assert caught.value.code == "SEARCH_TIMEOUT"
    assert caught.value.recoverable is True
    assert "secret upstream detail" not in str(caught.value)


def test_doubao_provider_marks_exhausted_quota_nonrecoverable():
    provider = DoubaoSearchProvider(
        api_key="test-key",
        client=_Client(
            error=DoubaoResponseError(
                "10406",
                "Free quota has been exhausted.",
            )
        ),
    )

    with pytest.raises(ExternalServiceError) as caught:
        provider.search("query")

    assert caught.value.code == "SEARCH_QUOTA_EXHAUSTED"
    assert caught.value.recoverable is False


def test_doubao_provider_requires_explicit_credentials(monkeypatch):
    monkeypatch.setenv(
        "ASK_ECHO_SEARCH_INFINITY_API_KEY",
        "must-not-be-read",
    )
    with pytest.raises(ValueError, match="ASK_ECHO_SEARCH_INFINITY_API_KEY"):
        DoubaoSearchProvider(api_key="", client=_Client())
