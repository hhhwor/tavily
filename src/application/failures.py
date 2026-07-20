"""应用服务共享的结构化失败构造器。"""
from __future__ import annotations

from typing import Optional

from src.domain.errors import ExternalServiceError, public_error_message
from src.domain.failures import SearchFailure


def search_failure(
    *,
    stage: str,
    source: str,
    code: str,
    message: object,
    source_type: Optional[str] = None,
    recoverable: bool = True,
) -> SearchFailure:
    external = message if isinstance(message, ExternalServiceError) else None
    return SearchFailure(
        stage=stage,
        source=source,
        type=source_type,
        code=external.code if external is not None else code,
        message=public_error_message(message),
        recoverable=external.recoverable if external is not None else recoverable,
    )
