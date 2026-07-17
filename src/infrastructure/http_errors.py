"""把 HTTP 客户端异常映射为不泄露请求细节的领域错误。"""
from __future__ import annotations

import re

import requests

from src.domain.errors import ExternalServiceError


def external_http_error(
    provider: str,
    operation: str,
    cause: BaseException,
) -> ExternalServiceError:
    """按超时、鉴权、限流和上游状态生成稳定错误码。"""
    prefix = re.sub(r"[^A-Z0-9]+", "_", operation.upper()).strip("_")
    suffix = "REQUEST_FAILED"
    recoverable = True
    if isinstance(cause, requests.Timeout):
        suffix = "TIMEOUT"
    elif isinstance(cause, requests.HTTPError):
        status = getattr(getattr(cause, "response", None), "status_code", None)
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
    )
