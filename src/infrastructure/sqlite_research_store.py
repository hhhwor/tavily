"""SQLite/WAL implementation of the research task state store."""
from __future__ import annotations

import sqlite3
from threading import RLock

from src.application.ports.research_store import (
    ResearchIdempotencyConflict,
    ResearchRevisionConflict,
    ResearchTaskNotFound,
)
from src.domain.research import ResearchTaskEnvelope
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
            CREATE TABLE IF NOT EXISTS research_seed_snapshots (
                research_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                FOREIGN KEY (research_id) REFERENCES research_tasks(research_id)
                    ON DELETE CASCADE
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
