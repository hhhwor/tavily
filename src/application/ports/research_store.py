"""Persistence port for durable research tasks."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, Sequence

from src.domain.evidence import Evidence
from src.domain.research import (
    ObjectivePlan,
    ResearchRoundCheckpoint,
    ResearchTaskEnvelope,
    RoundResult,
)
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

    def save_plan(
        self,
        research_id: str,
        *,
        attempt: int,
        plan: ObjectivePlan,
    ) -> None: ...

    def latest_plan(
        self,
        research_id: str,
    ) -> tuple[int, ObjectivePlan] | None: ...

    def checkpoint_round(
        self,
        checkpoint: ResearchRoundCheckpoint,
        evidence: Sequence[Evidence],
    ) -> None: ...

    def commit_evidence_set(
        self,
        research_id: str,
        *,
        evidence_set_revision: int,
        evidence: Sequence[Evidence],
        committed_at: datetime,
    ) -> None: ...

    def latest_checkpoint(
        self,
        research_id: str,
        *,
        attempt: int,
    ) -> tuple[ResearchRoundCheckpoint, list[Evidence]] | None: ...

    def list_rounds(self, research_id: str) -> list[RoundResult]: ...

    def append_event(
        self,
        research_id: str,
        *,
        attempt: int,
        kind: str,
        payload: dict[str, Any],
    ) -> None: ...

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
