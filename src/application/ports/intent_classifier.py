"""Model intent-classification boundary used by query planning."""
from __future__ import annotations

from typing import Protocol

from src.domain.intent import IntentDecision


class IntentClassifier(Protocol):
    def classify(self, query: str) -> IntentDecision:
        """Classify one normalized automatic-route query or raise an external error."""
        ...
