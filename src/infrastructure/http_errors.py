"""把 HTTP 客户端异常映射为不泄露请求细节的领域错误。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from src.domain.errors import ExternalServiceError


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def external_http_error(
    provider: str,
    operation: str,
    cause: BaseException,
) -> ExternalServiceError:
    """按超时、鉴权、限流和上游状态生成稳定错误码。"""
    prefix = re.sub(r"[^A-Z0-9]+", "_", operation.upper()).strip("_")
    suffix = "REQUEST_FAILED"
    recoverable = True
    retry_after_seconds = None
    if isinstance(cause, requests.Timeout):
        suffix = "TIMEOUT"
    elif isinstance(cause, requests.HTTPError):
        response = getattr(cause, "response", None)
        status = getattr(response, "status_code", None)
        retry_after_seconds = _retry_after_seconds(response)
        if status in {401, 403}:
            suffix = "AUTH_FAILED"
            recoverable = False
        elif status == 429:
            suffix = "RATE_LIMITED"
        elif isinstance(status, int) and status >= 500:
            suffix = "UPSTREAM_UNAVAILABLE"
        elif isinstance(status, int):
            suffix = "REQUEST_REJECTED"
            recoverable = False
    elif isinstance(cause, (ValueError, KeyError, TypeError)):
        suffix = "INVALID_RESPONSE"
    return ExternalServiceError(
        provider=provider,
        code=f"{prefix}_{suffix}",
        recoverable=recoverable,
        cause=cause,
        retry_after_seconds=retry_after_seconds,
    )
