"""Pure final evidence selection with explicit source-coverage preservation."""
from __future__ import annotations

from typing import Sequence

from src.domain.documents import DocumentKind
from src.domain.evidence import Evidence


def select_evidence(
    evidence: Sequence[Evidence],
    *,
    limit: int,
    required_source_types: Sequence[DocumentKind] = (),
) -> list[Evidence]:
    """Select globally ranked evidence while reserving explicit source coverage.

    When the response has enough slots, the best candidate from every explicitly
    requested source type is retained. Remaining slots follow the existing global
    order. If there are fewer slots than represented source types, global order
    remains the deterministic fallback.
    """
    ranked = list(evidence)
    if len(ranked) <= limit or not required_source_types:
        return ranked[:limit]

    required = set(required_source_types)
    reserved: set[int] = set()
    represented: set[DocumentKind] = set()
    for index, item in enumerate(ranked):
        if item.type in required and item.type not in represented:
            reserved.add(index)
            represented.add(item.type)

    if len(reserved) > limit:
        return ranked[:limit]

    selected = set(reserved)
    for index in range(len(ranked)):
        if len(selected) >= limit:
            break
        selected.add(index)
    return [item for index, item in enumerate(ranked) if index in selected]
