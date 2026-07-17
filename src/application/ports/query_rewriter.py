"""Query rewrite boundary used by application planning."""
from __future__ import annotations

from typing import Protocol


class QueryRewriter(Protocol):
    def rewrite(self, query: str, *, academic: bool = False) -> str:
        """Return a rewritten query or raise a sanitized external error."""
        ...


class NoOpQueryRewriter:
    def rewrite(self, query: str, *, academic: bool = False) -> str:
        return query
