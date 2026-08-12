"""法规检索结果的受限深读适配器。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Callable

from src.application.document_identity import legal_document_version_id
from src.application.research_execution import ExecutionContext
from src.domain.document_read import (
    DocumentChunk,
    DocumentReadDiagnostics,
    DocumentReadResult,
    DocumentVersion,
)
from src.domain.evidence import Evidence, EvidenceLocator


_PARSER_VERSION = "fy-law-snippet.v1"


class LegalDocumentReader:
    """将 FY 已返回的法规条文转为可定位的受限研究片段。

    FY MCP 当前只提供命中条文，不承诺整部法规全文或稳定官方 URL，故该
    reader 明确返回 excerpt-only、observed-chunks 的部分版本，不把片段误报为
    可复现的法规全文。
    """

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))

    def read(
        self,
        candidate: Evidence,
        *,
        context: ExecutionContext,
    ) -> DocumentReadResult:
        if candidate.type != "legal" or candidate.legal is None:
            return self._failure("DOCUMENT_TYPE_UNSUPPORTED")
        text = candidate.passage.text.strip()
        if not text:
            return self._failure("LEGAL_TEXT_MISSING")
        context.checkpoint()
        law = candidate.legal
        law_title = candidate.title.strip() or candidate.result_id
        item = law.item.strip()
        source_record_id = " ".join(value for value in (law_title, item) if value)
        content_hash = "sha256:" + hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()
        version_id = legal_document_version_id(law_title, item, content_hash)
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        directory = " / ".join(law.directory)
        version = DocumentVersion(
            document_version_id=version_id,
            independent_work_id=(
                f"legal:{law_title}|{item or candidate.result_id}"
            ),
            type="legal",
            source_record_id=source_record_id,
            canonical_uri="",
            content_hash=content_hash,
            content_hash_scope="observed_chunks",
            parser_version=_PARSER_VERSION,
            retrieved_at=now.astimezone(timezone.utc).isoformat(),
            complete=False,
            storage_mode="excerpt_only",
        )
        locator = EvidenceLocator(
            document_id=law_title,
            version_id=version_id,
            section=directory or None,
            paragraph_id=item or None,
            char_start=0,
            char_end=len(text),
            chunk_index=0,
        )
        chunk = DocumentChunk(
            document_version_id=version_id,
            chunk_index=0,
            text=text,
            text_hash=content_hash,
            locator=locator,
        )
        return DocumentReadResult(
            status="partial",
            version=version,
            chunks=[chunk],
            diagnostics=DocumentReadDiagnostics(
                warnings=["LEGAL_FULL_TEXT_NOT_AVAILABLE"],
                retryable=False,
            ),
            bytes_read=len(text.encode("utf-8")),
        )

    @staticmethod
    def _failure(code: str) -> DocumentReadResult:
        return DocumentReadResult(
            status="unavailable",
            diagnostics=DocumentReadDiagnostics(
                failure_code=code,
                message="法规文本无法作为可定位片段读取。",
                retryable=False,
            ),
        )
