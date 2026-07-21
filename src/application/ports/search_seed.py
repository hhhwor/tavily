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


def search_seed_snapshot_hash_matches(
    snapshot: SearchSeedSnapshot,
    expected: str,
) -> bool:
    """校验当前或旧版 seed hash，不允许新增字段借用旧 hash。"""
    if search_seed_snapshot_hash(snapshot) == expected:
        return True
    source_intent_fields = {
        "requested_source_types",
        "planned_source_types",
    }
    if source_intent_fields & snapshot.model_fields_set:
        return False
    legacy_payload = snapshot.model_dump(mode="json")
    for field_name in source_intent_fields:
        legacy_payload.pop(field_name, None)
    canonical = json.dumps(
        legacy_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return legacy_hash == expected


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
