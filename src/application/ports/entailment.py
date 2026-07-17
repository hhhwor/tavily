"""Entailment classification boundary."""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


class EntailmentClassifier(Protocol):
    name: str

    def classify_pairs(self, pairs: Sequence[Any]) -> Mapping[str, Any]: ...
