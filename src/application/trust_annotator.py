"""Evidence 可信标注与本次检索边界组装。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from src.models import Evidence, SearchBoundary
from src.trust import annotate_evidence, build_search_boundary


@dataclass(frozen=True)
class TrustOutcome:
    evidence: tuple[Evidence, ...]
    search_boundary: SearchBoundary | None


class TrustAnnotator:
    def __init__(self, snapshot_resolver: Callable[[str], str]) -> None:
        self._snapshot_resolver = snapshot_resolver

    def annotate(
        self,
        *,
        mode: str,
        query: str,
        planned_sources: Sequence[str],
        evidence: Sequence[Evidence],
        query_time: datetime,
        candidate_budget: int,
    ) -> TrustOutcome:
        items = list(evidence)
        if mode == "off":
            return TrustOutcome(tuple(items), None)
        if mode != "annotate":
            raise ValueError("trust_mode 仅支持 off / annotate")

        annotate_evidence(items)
        source_snapshot = {
            source: self._snapshot_resolver(source) for source in planned_sources
        }
        boundary = build_search_boundary(
            query=query,
            source_names=list(planned_sources),
            evidence=items,
            query_time=query_time,
            source_snapshot=source_snapshot,
            max_candidates=candidate_budget,
        )
        return TrustOutcome(tuple(items), boundary)
