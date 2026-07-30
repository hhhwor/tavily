import json

import pytest

from eval.doubao_search_client import fit_doubao_query, parse_doubao_response


def test_fit_query_preserves_short_query() -> None:
    query, adapted = fit_doubao_query("  latest   Premier League standings ")
    assert query == "latest Premier League standings"
    assert adapted is False


def test_fit_query_preserves_both_ends_at_api_boundary() -> None:
    original = (
        "Due to falling electricity prices in January 2023, the Pakistani "
        "government pushed back the mandated closing time of shopping malls "
        "from 9:30pm to what time of day?"
    )
    query, adapted = fit_doubao_query(original)
    assert len(query) <= 100
    assert query.startswith("Due to falling electricity")
    assert query.endswith("to what time of day?")
    assert adapted is True


def test_parse_response_normalizes_evidence_and_adaptation_gap() -> None:
    payload = {
        "Result": {
            "WebResults": [
                {
                    "Title": "Example",
                    "Url": "https://example.com",
                    "Summary": "Query-specific summary.",
                    "Snippet": "Short snippet.",
                    "Content": "Full content.",
                    "SiteName": "Example Site",
                    "PublishTime": "2026-07-29T00:00:00Z",
                    "AuthInfoLevel": 2,
                    "RankScore": 0.9,
                }
            ]
        }
    }
    response = parse_doubao_response(
        json.dumps(payload), query="adapted query", query_adapted=True, limit=8
    )
    assert response["status"] == "complete"
    assert response["evidence"][0]["source"] == "doubao"
    assert "Query-specific summary." in response["evidence"][0]["passage"]["text"]
    assert response["retrieval_assessment"]["gaps"][0]["code"] == (
        "QUERY_LENGTH_ADAPTED"
    )


def test_parse_response_rejects_missing_result() -> None:
    with pytest.raises(RuntimeError, match="no Result"):
        parse_doubao_response("{}", query="q", query_adapted=False, limit=8)
