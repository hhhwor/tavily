"""Injectable process runtime primitives."""
from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Deadline:
    expires_at: float
    clock: Clock

    @classmethod
    def after(cls, milliseconds: int, clock: Clock) -> "Deadline":
        return cls(
            expires_at=clock.monotonic() + max(0, milliseconds) / 1000,
            clock=clock,
        )

    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - self.clock.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining_seconds() <= 0
