"""Application cache boundary."""
from __future__ import annotations

from typing import Any, Protocol


class CacheBackend(Protocol):
    """Thread-safe key/value cache; implementations own expiry mechanics."""

    name: str

    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl: int) -> None: ...

    def stats(self) -> dict: ...
