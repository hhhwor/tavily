"""Patent claims/specification reader with family and citation identity."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable

from src.application.document_identity import patent_document_version_id
from src.application.ports.patent_text import PatentTextGateway
from src.application.research_execution import ExecutionContext
from src.domain.document_read import (
    DocumentChunk,
    DocumentReadDiagnostics,
    DocumentReadResult,
    DocumentVersion,
)
from src.domain.evidence import Evidence, EvidenceLocator


_PARSER_VERSION = "patent-fulltext.v1"


class PatentDocumentReader:
    def __init__(
        self,
        gateway: PatentTextGateway,
        *,
        max_units: int = 1000,
        max_unit_chars: int = 30_000,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._max_units = max(1, max_units)
        self._max_unit_chars = max(1, max_unit_chars)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def read(
        self,
        candidate: Evidence,
        *,
        context: ExecutionContext,
    ) -> DocumentReadResult:
        if candidate.type != "patent" or candidate.patent is None:
            return self._failure("DOCUMENT_TYPE_UNSUPPORTED", False)
        publication = candidate.patent.publication_number.strip()
        if not publication:
            return self._failure("PATENT_PUBLICATION_NUMBER_MISSING", False)
        context.checkpoint()
        record = self._gateway.fetch(
            publication,
            deadline=context.deadline,
        )
        if record.status != "ready" or not record.units:
            return self._failure(
                record.failure_code or "PATENT_FULLTEXT_UNAVAILABLE",
                record.retryable,
            )
        canonical_payload = json.dumps(
            record.model_dump(
                mode="json",
                exclude={
                    "status",
                    "canonical_uri",
                    "source_version_id",
                    "failure_code",
                    "retryable",
                },
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = "sha256:" + hashlib.sha256(
            canonical_payload.encode("utf-8")
        ).hexdigest()
        version_id = patent_document_version_id(publication, content_hash)
        independent = (
            record.family_id
            or record.priority_root
            or publication
        )
        retrieved_at = self._now()
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
        version = DocumentVersion(
            document_version_id=version_id,
            independent_work_id=f"patent-family:{independent}",
            type="patent",
            source_record_id=record.publication_number,
            source_version_id=record.source_version_id,
            canonical_uri=record.canonical_uri or candidate.url,
            content_hash=content_hash,
            content_hash_scope="extracted_text",
            parser_version=_PARSER_VERSION,
            retrieved_at=retrieved_at.astimezone(timezone.utc).isoformat(),
            complete=True,
            license=record.license,
            relations={
                "family_id": [record.family_id] if record.family_id else [],
                "family_members": list(record.family_members),
                "priority_root": [record.priority_root] if record.priority_root else [],
                "priority_dates": list(record.priority_dates),
                "application_date": (
                    [record.application_date] if record.application_date else []
                ),
                "publication_date": (
                    [record.publication_date] if record.publication_date else []
                ),
                "patent_citations": list(record.patent_citations),
                "npl_citations": list(record.npl_citations),
            },
        )
        chunks: list[DocumentChunk] = []
        warnings: list[str] = []
        for index, unit in enumerate(record.units[:self._max_units]):
            text = unit.text[:self._max_unit_chars]
            if len(text) < len(unit.text):
                warnings.append("PATENT_UNIT_TRUNCATED")
            locator = EvidenceLocator(
                document_id=record.publication_number,
                version_id=version_id,
                section=unit.section or (
                    "claims" if unit.kind == "claim" else "description"
                ),
                paragraph_id=(
                    unit.identifier if unit.kind == "description" else None
                ),
                claim_number=(
                    unit.identifier if unit.kind == "claim" else None
                ),
                char_start=0,
                char_end=len(text),
                chunk_index=index,
            )
            chunks.append(DocumentChunk(
                document_version_id=version_id,
                chunk_index=index,
                text=text,
                text_hash="sha256:" + hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
                locator=locator,
            ))
        if len(record.units) > self._max_units:
            warnings.append("PATENT_UNIT_LIMIT_EXCEEDED")
        return DocumentReadResult(
            status="ready",
            version=version,
            chunks=chunks,
            diagnostics=DocumentReadDiagnostics(
                warnings=list(dict.fromkeys(warnings)),
                message="Patent original document read completed.",
            ),
            bytes_read=sum(len(chunk.text.encode("utf-8")) for chunk in chunks),
        )

    @staticmethod
    def _failure(code: str, retryable: bool) -> DocumentReadResult:
        return DocumentReadResult(
            status="failed" if retryable else "unavailable",
            diagnostics=DocumentReadDiagnostics(
                warnings=[code],
                failure_code=code,
                message="Patent original document is unavailable.",
                retryable=retryable,
            ),
        )
