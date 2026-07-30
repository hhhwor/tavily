"""Application boundary for retry and circuit-breaker execution."""
from __future__ import annotations

from typing import Callable, Protocol, TypeVar

from src.application.ports.runtime import Deadline


T = TypeVar("T")


class ResiliencePolicy(Protocol):
    def call(
        self,
        dependency: str,
        operation: str,
        function: Callable[[], T],
        *,
        deadline: Deadline | None = None,
    ) -> T: ...

    def snapshot(self) -> dict[str, object]: ...
