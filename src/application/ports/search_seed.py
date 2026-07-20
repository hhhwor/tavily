"""Persistence port for immutable search research seeds."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from src.domain.search_api import SearchSeed, SearchSeedSnapshot


class SearchSeedNotFound(LookupError):
    pass


class SearchSeedExpired(LookupError):
    pass


class SearchSeedIntegrityError(RuntimeError):
    pass


def search_seed_snapshot_hash(snapshot: SearchSeedSnapshot) -> str:
    canonical = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredSearchSeed:
    seed: SearchSeed
    snapshot: SearchSeedSnapshot


class SearchSeedStore(Protocol):
    def save(
        self,
        snapshot: SearchSeedSnapshot,
        *,
        ttl_seconds: int,
    ) -> SearchSeed: ...

    def get(self, search_id: str) -> StoredSearchSeed: ...

    def close(self) -> None: ...
