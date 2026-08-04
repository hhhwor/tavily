"""Port for bounded original-document reads."""
from __future__ import annotations

from typing import Protocol

from src.application.research_execution import ExecutionContext
from src.domain.document_read import DocumentReadResult
from src.domain.evidence import Evidence


class DocumentReader(Protocol):
    def read(
        self,
        candidate: Evidence,
        *,
        context: ExecutionContext,
    ) -> DocumentReadResult: ...
