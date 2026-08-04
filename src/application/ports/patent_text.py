"""Port for retrieving authoritative patent full text and relationships."""
from __future__ import annotations

from typing import Protocol

from src.application.ports.runtime import Deadline
from src.domain.patent_text import PatentDocumentRecord


class PatentTextGateway(Protocol):
    def fetch(
        self,
        publication_number: str,
        *,
        deadline: Deadline,
    ) -> PatentDocumentRecord: ...


class UnavailablePatentTextGateway:
    def fetch(
        self,
        publication_number: str,
        *,
        deadline: Deadline,
    ) -> PatentDocumentRecord:
        return PatentDocumentRecord(
            status="unavailable",
            publication_number=publication_number,
            failure_code="PATENT_FULLTEXT_SOURCE_UNCONFIGURED",
            retryable=False,
        )
