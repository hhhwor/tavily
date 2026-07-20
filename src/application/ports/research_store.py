"""Persistence port for durable research tasks."""
from __future__ import annotations

from typing import Protocol

from src.domain.research import ResearchTaskEnvelope
from src.domain.search_api import SearchSeedSnapshot


class ResearchTaskNotFound(LookupError):
    pass


class ResearchIdempotencyConflict(ValueError):
    pass


class ResearchRevisionConflict(ValueError):
    pass


class ResearchStore(Protocol):
    def create(
        self,
        task: ResearchTaskEnvelope,
        *,
        idempotency_key: str,
        request_hash: str,
        seed_snapshot: SearchSeedSnapshot,
    ) -> tuple[ResearchTaskEnvelope, bool]: ...

    def get(self, research_id: str) -> ResearchTaskEnvelope: ...

    def find_by_idempotency(
        self,
        idempotency_key: str,
        request_hash: str,
    ) -> ResearchTaskEnvelope | None: ...

    def get_seed(self, research_id: str) -> SearchSeedSnapshot: ...

    def save(
        self,
        task: ResearchTaskEnvelope,
        *,
        expected_revision: int,
    ) -> ResearchTaskEnvelope: ...

    def cancel(
        self,
        task: ResearchTaskEnvelope,
        *,
        expected_revision: int,
    ) -> ResearchTaskEnvelope: ...

    def cancel_requested(self, research_id: str) -> bool: ...

    def runnable(self) -> list[str]: ...

    def close(self) -> None: ...
