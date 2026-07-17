"""F05 外部错误与凭证脱敏契约。"""
from __future__ import annotations

import requests
import pytest

from src.application.failures import search_failure
from src.domain.errors import ExternalServiceError, redact_sensitive
from src.providers.serpapi import SerpApiProvider


class _LeakyHttp:
    def get(self, *args, **kwargs):
        response = requests.Response()
        response.status_code = 401
        response.url = (
            "https://serpapi.com/search?api_key=super-secret-key&q=private-query"
        )
        error = requests.HTTPError(
            f"401 Client Error for url: {response.url}",
            response=response,
        )
        raise error


def test_provider_keeps_raw_cause_but_exposes_only_stable_error():
    provider = SerpApiProvider(
        api_key="super-secret-key",
        http_session=_LeakyHttp(),
    )

    with pytest.raises(ExternalServiceError) as caught:
        provider.search("private-query")

    error = caught.value
    assert error.provider == "serpapi"
    assert error.code == "SEARCH_AUTH_FAILED"
    assert error.recoverable is False
    assert error.cause is not None
    assert "super-secret-key" in str(error.cause)
    assert "super-secret-key" not in str(error)
    assert "private-query" not in str(error)
    assert "super-secret-key" not in repr(error)

    failure = search_failure(
        stage="provider_search",
        source="serpapi",
        source_type="web",
        code="PROVIDER_SEARCH_FAILED",
        message=error,
    )
    assert failure.code == "SEARCH_AUTH_FAILED"
    assert failure.recoverable is False
    assert "super-secret-key" not in failure.message
    assert "private-query" not in failure.message


def test_last_resort_redaction_removes_urls_headers_and_secret_pairs():
    unsafe = (
        "GET https://example.test/search?api_key=abc&q=private "
        "Authorization: Bearer bearer-secret token=plain-secret"
    )
    safe = redact_sensitive(unsafe)

    assert "abc" not in safe
    assert "private" not in safe
    assert "bearer-secret" not in safe
    assert "plain-secret" not in safe
    assert "[REDACTED]" in safe

    generic = search_failure(
        stage="rerank",
        source="model",
        source_type="web",
        code="RERANK_FAILED",
        message=RuntimeError(unsafe),
    )
    assert generic.message == "operation failed; see failure code"
    assert "private" not in generic.message
