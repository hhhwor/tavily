"""SQLite/WAL implementation of the research task state store."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Sequence

from src.application.ports.research_store import (
    ResearchIdempotencyConflict,
    ResearchRevisionConflict,
    ResearchTaskNotFound,
    StoredResearchArtifact,
)
from src.domain.evidence import Evidence
from src.domain.document_read import DocumentChunk, DocumentReadResult
from src.domain.evidence import EvidenceLocator
from src.domain.research import (
    ObjectivePlan,
    ResearchArtifact,
    ResearchRoundCheckpoint,
    ResearchTaskEnvelope,
    ResearchSynthesisSnapshot,
    RoundResult,
)
from src.domain.search_api import SearchSeedSnapshot


class SqliteResearchStore:
    def __init__(self, path: str) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_tasks (
                research_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                request_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                task_revision INTEGER NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_syntheses (
                research_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (research_id, operation_id),
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_artifacts (
                research_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                metadata TEXT NOT NULL,
                content BLOB NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (research_id, artifact_id),
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_seed_snapshots (
                research_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_attempts (
                research_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                plan_revision INTEGER NOT NULL,
                plan TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (research_id, attempt),
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_evidence_sets (
                research_id TEXT NOT NULL,
                evidence_set_revision INTEGER NOT NULL,
                payload TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                PRIMARY KEY (research_id, evidence_set_revision),
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_rounds (
                research_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                evidence_set_revision INTEGER NOT NULL,
                payload TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                PRIMARY KEY (research_id, attempt, round_number),
                FOREIGN KEY (research_id, attempt)
                    REFERENCES research_attempts(research_id, attempt)
                    ON DELETE CASCADE,
                FOREIGN KEY (research_id, evidence_set_revision)
                    REFERENCES research_evidence_sets(
                        research_id, evidence_set_revision
                    ) ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                research_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_document_reads (
                research_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                document_version_id TEXT,
                independent_work_id TEXT,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (research_id, action_id),
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_document_chunks (
                research_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                document_version_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (research_id, action_id, chunk_index),
                FOREIGN KEY (research_id, action_id)
                    REFERENCES research_document_reads(research_id, action_id)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_research_document_locator
            ON research_document_chunks(
                research_id, document_version_id, chunk_index
            )
            """
        )

    def create(
        self,
        task: ResearchTaskEnvelope,
        *,
        idempotency_key: str,
        request_hash: str,
        seed_snapshot: SearchSeedSnapshot,
    ) -> tuple[ResearchTaskEnvelope, bool]:
        payload = task.model_dump_json()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """
                    SELECT request_hash, payload FROM research_tasks
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise ResearchIdempotencyConflict(
                            "同一 Idempotency-Key 对应了不同研究请求"
                        )
                    self._connection.execute("COMMIT")
                    return (
                        ResearchTaskEnvelope.model_validate_json(existing["payload"]),
                        False,
                    )
                self._connection.execute(
                    """
                    INSERT INTO research_tasks
                        (research_id, idempotency_key, request_hash, state,
                         task_revision, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.research_id,
                        idempotency_key,
                        request_hash,
                        task.state,
                        task.task_revision,
                        payload,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO research_seed_snapshots (research_id, payload)
                    VALUES (?, ?)
                    """,
                    (task.research_id, seed_snapshot.model_dump_json()),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return task, True

    def get(self, research_id: str) -> ResearchTaskEnvelope:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM research_tasks WHERE research_id = ?",
                (research_id,),
            ).fetchone()
        if row is None:
            raise ResearchTaskNotFound(research_id)
        return ResearchTaskEnvelope.model_validate_json(row["payload"])

    def find_by_idempotency(
        self,
        idempotency_key: str,
        request_hash: str,
    ) -> ResearchTaskEnvelope | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT request_hash, payload FROM research_tasks
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ResearchIdempotencyConflict(
                "同一 Idempotency-Key 对应了不同研究请求"
            )
        return ResearchTaskEnvelope.model_validate_json(row["payload"])

    def get_seed(self, research_id: str) -> SearchSeedSnapshot:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload FROM research_seed_snapshots WHERE research_id = ?
                """,
                (research_id,),
            ).fetchone()
        if row is None:
            raise ResearchTaskNotFound(research_id)
        return SearchSeedSnapshot.model_validate_json(row["payload"])

    def save_plan(
        self,
        research_id: str,
        *,
        attempt: int,
        plan: ObjectivePlan,
    ) -> None:
        if not self._exists(research_id):
            raise ResearchTaskNotFound(research_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO research_attempts
                    (research_id, attempt, plan_revision, plan, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(research_id, attempt) DO UPDATE SET
                    plan_revision = excluded.plan_revision,
                    plan = excluded.plan
                """,
                (
                    research_id,
                    attempt,
                    plan.revision,
                    plan.model_dump_json(),
                    now,
                ),
            )

    def latest_plan(
        self,
        research_id: str,
    ) -> tuple[int, ObjectivePlan] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT attempt, plan FROM research_attempts
                WHERE research_id = ?
                ORDER BY attempt DESC
                LIMIT 1
                """,
                (research_id,),
            ).fetchone()
        if row is None:
            return None
        return int(row["attempt"]), ObjectivePlan.model_validate_json(row["plan"])

    def checkpoint_round(
        self,
        checkpoint: ResearchRoundCheckpoint,
        evidence: Sequence[Evidence],
    ) -> None:
        evidence_payload = json.dumps(
            [item.model_dump(mode="json") for item in evidence],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        committed_at = checkpoint.committed_at.isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                attempt = self._connection.execute(
                    """
                    SELECT 1 FROM research_attempts
                    WHERE research_id = ? AND attempt = ?
                    """,
                    (checkpoint.research_id, checkpoint.attempt),
                ).fetchone()
                if attempt is None:
                    raise ResearchTaskNotFound(checkpoint.research_id)
                self._connection.execute(
                    """
                    INSERT INTO research_evidence_sets
                        (research_id, evidence_set_revision, payload, committed_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(research_id, evidence_set_revision) DO UPDATE SET
                        payload = excluded.payload,
                        committed_at = excluded.committed_at
                    """,
                    (
                        checkpoint.research_id,
                        checkpoint.evidence_set_revision,
                        evidence_payload,
                        committed_at,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO research_rounds
                        (research_id, attempt, round_number,
                         evidence_set_revision, payload, committed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(research_id, attempt, round_number) DO UPDATE SET
                        evidence_set_revision = excluded.evidence_set_revision,
                        payload = excluded.payload,
                        committed_at = excluded.committed_at
                    """,
                    (
                        checkpoint.research_id,
                        checkpoint.attempt,
                        checkpoint.round,
                        checkpoint.evidence_set_revision,
                        checkpoint.model_dump_json(),
                        committed_at,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO research_events
                        (research_id, attempt, kind, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.research_id,
                        checkpoint.attempt,
                        "round_checkpointed",
                        json.dumps(
                            {
                                "round": checkpoint.round,
                                "evidence_set_revision": (
                                    checkpoint.evidence_set_revision
                                ),
                                "gain": checkpoint.result.gain.score,
                            },
                            separators=(",", ":"),
                        ),
                        committed_at,
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def commit_evidence_set(
        self,
        research_id: str,
        *,
        evidence_set_revision: int,
        evidence: Sequence[Evidence],
        committed_at: datetime,
    ) -> None:
        if not self._exists(research_id):
            raise ResearchTaskNotFound(research_id)
        payload = json.dumps(
            [item.model_dump(mode="json") for item in evidence],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO research_evidence_sets
                    (research_id, evidence_set_revision, payload, committed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(research_id, evidence_set_revision) DO UPDATE SET
                    payload = excluded.payload,
                    committed_at = excluded.committed_at
                """,
                (
                    research_id,
                    evidence_set_revision,
                    payload,
                    committed_at.isoformat(),
                ),
            )

    def latest_checkpoint(
        self,
        research_id: str,
        *,
        attempt: int,
    ) -> tuple[ResearchRoundCheckpoint, list[Evidence]] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT rounds.payload AS checkpoint_payload,
                       evidence.payload AS evidence_payload
                FROM research_rounds AS rounds
                JOIN research_evidence_sets AS evidence
                  ON evidence.research_id = rounds.research_id
                 AND evidence.evidence_set_revision =
                     rounds.evidence_set_revision
                WHERE rounds.research_id = ? AND rounds.attempt = ?
                ORDER BY rounds.round_number DESC
                LIMIT 1
                """,
                (research_id, attempt),
            ).fetchone()
        if row is None:
            return None
        checkpoint = ResearchRoundCheckpoint.model_validate_json(
            row["checkpoint_payload"]
        )
        evidence = [
            Evidence.model_validate(item)
            for item in json.loads(row["evidence_payload"])
        ]
        return checkpoint, evidence

    def list_rounds(self, research_id: str) -> list[RoundResult]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload FROM research_rounds
                WHERE research_id = ?
                ORDER BY attempt, round_number
                """,
                (research_id,),
            ).fetchall()
        return [
            ResearchRoundCheckpoint.model_validate_json(row["payload"]).result
            for row in rows
        ]

    def append_event(
        self,
        research_id: str,
        *,
        attempt: int,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        if not self._exists(research_id):
            raise ResearchTaskNotFound(research_id)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO research_events
                    (research_id, attempt, kind, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    research_id,
                    attempt,
                    kind,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def save_document_read(
        self,
        research_id: str,
        *,
        attempt: int,
        action_id: str,
        result: DocumentReadResult,
    ) -> None:
        if not self._exists(research_id):
            raise ResearchTaskNotFound(research_id)
        version = result.version
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(
            result.model_dump(mode="json", exclude={"chunks"}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO research_document_reads
                        (research_id, action_id, attempt, document_version_id,
                         independent_work_id, status, payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(research_id, action_id) DO UPDATE SET
                        attempt = excluded.attempt,
                        document_version_id = excluded.document_version_id,
                        independent_work_id = excluded.independent_work_id,
                        status = excluded.status,
                        payload = excluded.payload,
                        created_at = excluded.created_at
                    """,
                    (
                        research_id,
                        action_id,
                        attempt,
                        version.document_version_id if version else None,
                        version.independent_work_id if version else None,
                        result.status,
                        payload,
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    DELETE FROM research_document_chunks
                    WHERE research_id = ? AND action_id = ?
                    """,
                    (research_id, action_id),
                )
                if version is not None and version.storage_mode == "full_text":
                    for chunk in result.chunks:
                        chunk_payload = json.dumps(
                            chunk.model_dump(mode="json", exclude={"text"}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        self._connection.execute(
                            """
                            INSERT INTO research_document_chunks
                                (research_id, action_id, document_version_id,
                                 chunk_index, text, payload)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                research_id,
                                action_id,
                                version.document_version_id,
                                chunk.chunk_index,
                                chunk.text,
                                chunk_payload,
                            ),
                        )
                self._connection.execute(
                    """
                    INSERT INTO research_events
                        (research_id, attempt, kind, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        research_id,
                        attempt,
                        "document_read_saved",
                        json.dumps(
                            {
                                "action_id": action_id,
                                "status": result.status,
                                "document_version_id": (
                                    version.document_version_id
                                    if version else None
                                ),
                                "chunks": len(result.chunks),
                                "failure_code": (
                                    result.diagnostics.failure_code
                                ),
                            },
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def get_document_read(
        self,
        research_id: str,
        *,
        action_id: str,
    ) -> DocumentReadResult | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload FROM research_document_reads
                WHERE research_id = ? AND action_id = ?
                """,
                (research_id, action_id),
            ).fetchone()
            chunk_rows = self._connection.execute(
                """
                SELECT text, payload FROM research_document_chunks
                WHERE research_id = ? AND action_id = ?
                ORDER BY chunk_index
                """,
                (research_id, action_id),
            ).fetchall()
        if row is None:
            return None
        data = json.loads(row["payload"])
        data["chunks"] = [
            DocumentChunk.model_validate({
                **json.loads(chunk_row["payload"]),
                "text": chunk_row["text"],
            }).model_dump(mode="python")
            for chunk_row in chunk_rows
        ]
        return DocumentReadResult.model_validate(data)

    def resolve_locator(
        self,
        research_id: str,
        locator: EvidenceLocator,
    ) -> str | None:
        if not locator.version_id or locator.chunk_index is None:
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT text FROM research_document_chunks
                WHERE research_id = ? AND document_version_id = ?
                  AND chunk_index = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (
                    research_id,
                    locator.version_id,
                    locator.chunk_index,
                ),
            ).fetchone()
        if row is None:
            return None
        text = str(row["text"])
        start = locator.char_start if locator.char_start is not None else 0
        end = locator.char_end if locator.char_end is not None else len(text)
        if start < 0 or end < start or end > len(text):
            return None
        return text[start:end]

    def begin_synthesis(
        self,
        research_id: str,
        *,
        attempt: int,
        snapshot: ResearchSynthesisSnapshot,
    ) -> bool:
        if snapshot.status != "pending":
            raise ValueError("begin_synthesis 只接受 pending snapshot")
        if not self._exists(research_id):
            raise ResearchTaskNotFound(research_id)
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO research_syntheses
                    (research_id, operation_id, attempt, status, payload,
                     created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    research_id,
                    snapshot.operation_id,
                    attempt,
                    snapshot.status,
                    snapshot.model_dump_json(),
                    snapshot.created_at.isoformat(),
                    None,
                ),
            )
        return cursor.rowcount == 1

    def get_synthesis(
        self,
        research_id: str,
        *,
        operation_id: str,
    ) -> ResearchSynthesisSnapshot | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload FROM research_syntheses
                WHERE research_id = ? AND operation_id = ?
                """,
                (research_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        return ResearchSynthesisSnapshot.model_validate_json(row["payload"])

    def save_synthesis(
        self,
        research_id: str,
        *,
        attempt: int,
        snapshot: ResearchSynthesisSnapshot,
    ) -> None:
        if not self._exists(research_id):
            raise ResearchTaskNotFound(research_id)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO research_syntheses
                    (research_id, operation_id, attempt, status, payload,
                     created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(research_id, operation_id) DO UPDATE SET
                    attempt = excluded.attempt,
                    status = excluded.status,
                    payload = excluded.payload,
                    completed_at = excluded.completed_at
                """,
                (
                    research_id,
                    snapshot.operation_id,
                    attempt,
                    snapshot.status,
                    snapshot.model_dump_json(),
                    snapshot.created_at.isoformat(),
                    (
                        snapshot.completed_at.isoformat()
                        if snapshot.completed_at is not None else None
                    ),
                ),
            )

    def save_artifact(
        self,
        research_id: str,
        *,
        metadata: ResearchArtifact,
        content: bytes,
    ) -> None:
        if not self._exists(research_id):
            raise ResearchTaskNotFound(research_id)
        if len(content) != metadata.size_bytes:
            raise ValueError("artifact content size 与 metadata 不一致")
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO research_artifacts
                    (research_id, artifact_id, kind, metadata, content,
                     expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(research_id, artifact_id) DO UPDATE SET
                    kind = excluded.kind,
                    metadata = excluded.metadata,
                    content = excluded.content,
                    expires_at = excluded.expires_at
                """,
                (
                    research_id,
                    metadata.artifact_id,
                    metadata.kind,
                    metadata.model_dump_json(),
                    sqlite3.Binary(content),
                    metadata.expires_at.isoformat(),
                ),
            )

    def get_artifact(
        self,
        research_id: str,
        *,
        artifact_id: str,
    ) -> StoredResearchArtifact | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT metadata, content FROM research_artifacts
                WHERE research_id = ? AND artifact_id = ?
                """,
                (research_id, artifact_id),
            ).fetchone()
        if row is None:
            return None
        return StoredResearchArtifact(
            metadata=ResearchArtifact.model_validate_json(row["metadata"]),
            content=bytes(row["content"]),
        )

    def save(
        self,
        task: ResearchTaskEnvelope,
        *,
        expected_revision: int,
    ) -> ResearchTaskEnvelope:
        if task.task_revision != expected_revision + 1:
            raise ValueError("保存任务时 task_revision 必须恰好递增 1")
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE research_tasks
                SET state = ?, task_revision = ?, payload = ?
                WHERE research_id = ? AND task_revision = ?
                """,
                (
                    task.state,
                    task.task_revision,
                    task.model_dump_json(),
                    task.research_id,
                    expected_revision,
                ),
            )
        if cursor.rowcount != 1:
            if self._exists(task.research_id):
                raise ResearchRevisionConflict(task.research_id)
            raise ResearchTaskNotFound(task.research_id)
        return task

    def _exists(self, research_id: str) -> bool:
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM research_tasks WHERE research_id = ?",
                (research_id,),
            ).fetchone() is not None

    def cancel(
        self,
        task: ResearchTaskEnvelope,
        *,
        expected_revision: int,
    ) -> ResearchTaskEnvelope:
        if task.task_revision != expected_revision + 1:
            raise ValueError("取消任务时 task_revision 必须恰好递增 1")
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE research_tasks
                SET state = ?, task_revision = ?, payload = ?, cancel_requested = 1
                WHERE research_id = ? AND task_revision = ?
                """,
                (
                    task.state,
                    task.task_revision,
                    task.model_dump_json(),
                    task.research_id,
                    expected_revision,
                ),
            )
        if cursor.rowcount != 1:
            if self._exists(task.research_id):
                raise ResearchRevisionConflict(task.research_id)
            raise ResearchTaskNotFound(task.research_id)
        return task

    def cancel_requested(self, research_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT cancel_requested FROM research_tasks
                WHERE research_id = ?
                """,
                (research_id,),
            ).fetchone()
        if row is None:
            raise ResearchTaskNotFound(research_id)
        return bool(row["cancel_requested"])

    def runnable(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT research_id FROM research_tasks
                WHERE state IN ('queued', 'running') AND cancel_requested = 0
                ORDER BY rowid
                """
            ).fetchall()
        return [row["research_id"] for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
