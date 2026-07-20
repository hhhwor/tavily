"""SQLite-backed immutable search seed store."""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import RLock

from src.application.ports.search_seed import (
    SearchSeedExpired,
    SearchSeedIntegrityError,
    SearchSeedNotFound,
    StoredSearchSeed,
    search_seed_snapshot_hash,
)
from src.domain.search_api import SearchSeed, SearchSeedSnapshot


class SqliteSearchSeedStore:
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
            CREATE TABLE IF NOT EXISTS search_seeds (
                search_id TEXT PRIMARY KEY,
                snapshot_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )

    def save(
        self,
        snapshot: SearchSeedSnapshot,
        *,
        ttl_seconds: int,
    ) -> SearchSeed:
        canonical = json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = search_seed_snapshot_hash(snapshot)
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=max(1, ttl_seconds))
        search_id = "srch_" + secrets.token_urlsafe(18)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO search_seeds
                    (search_id, snapshot_hash, created_at, expires_at, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    search_id,
                    digest,
                    created_at.isoformat(),
                    expires_at.isoformat(),
                    canonical,
                ),
            )
        return SearchSeed(
            search_id=search_id,
            created_at=created_at,
            expires_at=expires_at,
            evidence_count=len(snapshot.evidence),
            seed_snapshot_hash=digest,
        )

    def get(self, search_id: str) -> StoredSearchSeed:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM search_seeds WHERE search_id = ?",
                (search_id,),
            ).fetchone()
        if row is None:
            raise SearchSeedNotFound(search_id)
        created_at = datetime.fromisoformat(row["created_at"])
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= datetime.now(timezone.utc):
            with self._lock:
                self._connection.execute(
                    "DELETE FROM search_seeds WHERE search_id = ?",
                    (search_id,),
                )
            raise SearchSeedExpired(search_id)
        snapshot = SearchSeedSnapshot.model_validate_json(row["payload"])
        if search_seed_snapshot_hash(snapshot) != row["snapshot_hash"]:
            raise SearchSeedIntegrityError(search_id)
        return StoredSearchSeed(
            seed=SearchSeed(
                search_id=search_id,
                created_at=created_at,
                expires_at=expires_at,
                evidence_count=len(snapshot.evidence),
                seed_snapshot_hash=row["snapshot_hash"],
            ),
            snapshot=snapshot,
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
