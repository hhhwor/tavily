"""Aliyun WebSearch provider and opt-in configuration tests."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from src.application.ports.retrieval import RetrievalRequest
from src.config import Settings
from src.domain.errors import ExternalServiceError
from src.providers.aliyun import AliyunWebSearchProvider


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)

    def json(self):
        return self._body


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _provider(session):
    return AliyunWebSearchProvider(
        access_key_id="test-id",
        access_key_secret="test-secret",
        http_session=session,
    )


def test_signed_search_normalizes_results_without_leaking_credentials():
    session = _Session(_Response({
        "code": 200,
        "data": {
            "total": 1,
            "result": [{
                "title": "Example",
                "url": "https://example.test/page",
                "snippet": "Evidence text",
                "date": "2026-07-28",
                "source": {"name": "Example", "domain": "example.test"},
            }],
        },
    }))
    provider = _provider(session)

    results = provider.search("fresh query", top_k=8)

    assert len(results) == 1
    assert results[0].source == "aliyun"
    assert results[0].content == "Evidence text"
    assert results[0].site == "Example"
    _, call = session.calls[0]
    payload = call["data"]
    assert json.loads(payload) == {
        "query": "fresh query",
        "limit": 8,
        "searchType": "pro",
        "region": "global",
    }
    assert call["headers"]["x-acs-content-sha256"] == hashlib.sha256(
        payload
    ).hexdigest()
    authorization = call["headers"]["authorization"]
    assert authorization.startswith("ACS3-HMAC-SHA256 Credential=test-id,")
    assert "test-secret" not in authorization


def test_request_applies_exact_time_window_and_caps_limit():
    session = _Session(_Response({"code": 200, "data": {"result": []}}))
    provider = _provider(session)
    request = RetrievalRequest(
        query="query",
        candidate_budget=80,
        time_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        time_to=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    batch = provider.retrieve(request)

    assert batch.actual_filters.to_dict() == {
        "limit": 50,
        "searchType": "pro",
        "region": "global",
        "startTime": "2026-07-01",
        "endTime": "2026-07-20",
    }
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["startTime"] == "2026-07-01"
    assert payload["endTime"] == "2026-07-20"
    assert payload["limit"] == 50


def test_business_error_is_mapped_to_stable_domain_error():
    provider = _provider(_Session(_Response({
        "code": 403,
        "message": "credential detail must not be public",
        "data": None,
    })))

    with pytest.raises(ExternalServiceError) as caught:
        provider.search("query")

    assert caught.value.code == "SEARCH_UPSTREAM_REJECTED"
    assert "credential detail" not in str(caught.value)


def test_aliyun_credentials_enable_by_default_and_can_be_disabled():
    enabled = Settings.from_env({
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "test-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test-secret",
        "OPENALEX_ENABLED": "false",
    })
    disabled = Settings.from_env({
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "test-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test-secret",
        "ALIYUN_WEB_SEARCH_ENABLED": "false",
        "OPENALEX_ENABLED": "false",
    })

    assert disabled.enabled_providers == ()
    assert enabled.enabled_providers == ("aliyun",)
    assert "test-id" not in repr(enabled)
    assert "test-secret" not in repr(enabled)
    with pytest.raises(ValueError, match="AccessKey"):
        Settings.from_env({"ALIYUN_WEB_SEARCH_ENABLED": "true"})
