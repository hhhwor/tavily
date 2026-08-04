"""Research execution controls shared by every expensive stage."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event, RLock
from typing import Callable, Mapping

from src.application.ports.runtime import Deadline, DeadlineExceededError
from src.domain.research import ResearchPrivacy


class ResearchCancelledError(RuntimeError):
    """Raised at a cooperative cancellation checkpoint."""


class BudgetExceededError(RuntimeError):
    """Raised before work starts when its hard budget cannot be reserved."""

    def __init__(self, category: str, requested: int, remaining: int) -> None:
        self.category = category
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"research budget exceeded: {category} requested={requested} "
            f"remaining={remaining}"
        )


class CancellationToken:
    """Thread-safe cancellation token with an optional durable-state probe."""

    def __init__(self, probe: Callable[[], bool] | None = None) -> None:
        self._event = Event()
        self._probe = probe

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(self._probe and self._probe())

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ResearchCancelledError("research execution was cancelled")


class BudgetReservation:
    """A reservation that must be committed or released exactly once."""

    def __init__(
        self,
        ledger: "BudgetLedger",
        category: str,
        amount: int,
    ) -> None:
        self._ledger = ledger
        self.category = category
        self.amount = amount
        self._closed = False

    def commit(self, actual: int | None = None) -> None:
        if self._closed:
            raise RuntimeError("budget reservation is already closed")
        actual_amount = self.amount if actual is None else actual
        if not 0 <= actual_amount <= self.amount:
            raise ValueError("actual usage must be within the reserved amount")
        self._ledger._commit(self.category, self.amount, actual_amount)
        self._closed = True

    def release(self) -> None:
        if self._closed:
            return
        self._ledger._release(self.category, self.amount)
        self._closed = True

    def __enter__(self) -> "BudgetReservation":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._closed:
            self.release()


class BudgetLedger:
    """Atomic hard-limit reservations plus auditable execution usage."""

    def __init__(
        self,
        limits: Mapping[str, int | None] | None = None,
        *,
        monotonic: Callable[[], float],
        initial_usage: Mapping[str, int] | None = None,
    ) -> None:
        self._limits = {
            key: max(0, int(value)) if value is not None else None
            for key, value in (limits or {}).items()
        }
        restored = {
            key: max(0, int(value))
            for key, value in (initial_usage or {}).items()
        }
        self._initial_elapsed_ms = restored.pop("elapsed_ms", 0)
        self._used: dict[str, int] = restored
        self._reserved: dict[str, int] = {}
        self._lock = RLock()
        self._monotonic = monotonic
        self._started_at = monotonic()

    def reserve(self, category: str, amount: int = 1) -> BudgetReservation:
        if amount < 0:
            raise ValueError("budget reservation amount must be non-negative")
        with self._lock:
            remaining = self.remaining(category)
            if remaining is not None and amount > remaining:
                raise BudgetExceededError(category, amount, remaining)
            self._reserved[category] = self._reserved.get(category, 0) + amount
        return BudgetReservation(self, category, amount)

    def consume(self, category: str, amount: int = 1) -> None:
        reservation = self.reserve(category, amount)
        reservation.commit()

    def remaining(self, category: str) -> int | None:
        with self._lock:
            limit = self._limits.get(category)
            if limit is None:
                return None
            return max(
                0,
                limit
                - self._used.get(category, 0)
                - self._reserved.get(category, 0),
            )

    def used(self, category: str) -> int:
        with self._lock:
            return self._used.get(category, 0)

    def _commit(self, category: str, reserved: int, actual: int) -> None:
        with self._lock:
            current = self._reserved.get(category, 0)
            if current < reserved:
                raise RuntimeError("budget reservation accounting underflow")
            self._reserved[category] = current - reserved
            self._used[category] = self._used.get(category, 0) + actual

    def _release(self, category: str, amount: int) -> None:
        with self._lock:
            current = self._reserved.get(category, 0)
            if current < amount:
                raise RuntimeError("budget reservation accounting underflow")
            self._reserved[category] = current - amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            usage = dict(self._used)
        usage["elapsed_ms"] = max(
            0,
            self._initial_elapsed_ms
            + int((self._monotonic() - self._started_at) * 1000),
        )
        return usage


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    research_id: str
    attempt: int
    policy_id: str
    privacy: ResearchPrivacy
    deadline: Deadline
    cancellation: CancellationToken
    budget: BudgetLedger
    principal_id: str = "system"

    def checkpoint(self) -> None:
        self.cancellation.raise_if_cancelled()
        if self.deadline.expired:
            raise DeadlineExceededError("research deadline exceeded")
