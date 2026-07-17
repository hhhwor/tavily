"""可安全跨应用边界传递的外部服务错误。"""
from __future__ import annotations

import re
from typing import Optional


_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_PAIR = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|secret(?:_key|_id)?|token)"
    r"(\s*[:=]\s*|%3[dD])([^\s,;&}\]]+)",
)
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)


def redact_sensitive(value: object, *, limit: int = 500) -> str:
    """对受控文本做最后一道凭证与 URL 查询参数脱敏。"""
    text = str(value)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = _URL_QUERY.sub(r"\1?[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _SECRET_PAIR.sub(r"\1\2[REDACTED]", text)
    return text[:limit]


class ExternalServiceError(RuntimeError):
    """外部服务失败的稳定公开表示。

    ``cause`` 只供受控服务端诊断使用；``str``/``repr`` 均不会包含第三方
    URL、响应体、请求参数或 header。
    """

    def __init__(
        self,
        *,
        provider: str,
        code: str,
        recoverable: bool = True,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.provider = provider
        self.code = code
        self.recoverable = recoverable
        self.cause = cause
        super().__init__(f"{provider} external service failed ({code})")

    def __repr__(self) -> str:
        return (
            f"ExternalServiceError(provider={self.provider!r}, code={self.code!r}, "
            f"recoverable={self.recoverable!r})"
        )


def public_error_message(value: object, *, limit: int = 500) -> str:
    """把异常或受控消息转换为可返回给客户端的文本。"""
    if isinstance(value, ExternalServiceError):
        return str(value)[:limit]
    if isinstance(value, BaseException):
        return "operation failed; see failure code"
    return redact_sensitive(value, limit=limit)
