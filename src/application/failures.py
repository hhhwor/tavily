"""应用服务共享的结构化失败构造器。"""
from __future__ import annotations

from typing import Optional

from src.models import SearchFailure


def search_failure(
    *,
    stage: str,
    source: str,
    source_type: Optional[str],
    code: str,
    message: object,
    recoverable: bool = True,
) -> SearchFailure:
    return SearchFailure(
        stage=stage,
        source=source,
        type=source_type,
        code=code,
        message=str(message)[:500],
        recoverable=recoverable,
    )
