"""Mixed-source final selection and per-stage count contracts."""
from __future__ import annotations

from datetime import timedelta

from src.application.answerability import AnswerabilityPolicy
from src.application.commands import SearchCommand
from src.application.evidence_assembler import EvidenceAssembler
from src.application.outcomes import (
    DiscoveryOutcome,
    PlannedQuery,
    RankingOutcome,
    RecallOutcome,
)
from src.application.search_service import SearchService
from src.application.trust_annotator import TrustOutcome
from src.domain.documents import DocumentKind, RankedDocument, RetrievedDocument
from src.domain.search import AcademicResult, PatentResult, SearchPlan, SearchResult
from src.domain.search_api import SearchSeed
from src.infrastructure.runtime import SystemClock
from src.pipeline.ranking_options import resolve_ranking_options


def _documents(
    kind: DocumentKind,
    count: int,
    *,
    first_score: float,
) -> tuple[tuple[RetrievedDocument, ...], tuple[RankedDocument, ...]]:
    retrieved = []
    ranked = []
    for index in range(count):
        common = {
            "url": f"https://example.test/{kind}/{index}",
            "title": f"{kind} {index}",
            "content": f"{kind} evidence {index}",
            "source": f"{kind}-source",
        }
        if kind == "academic":
            result = AcademicResult(**common, work_id=f"W{index}")
        elif kind == "patent":
            result = PatentResult(**common, publication_number=f"P{index}")
        else:
            result = SearchResult(**common)
        document = RetrievedDocument.from_result(result, kind)
        retrieved.append(document)
        ranked.append(RankedDocument(
            document=document,
            score=first_score - index * 0.01,
            ranking_profile="quality",
        ))
    return tuple(retrieved), tuple(ranked)


def _execute(limit: int):
    recalled_web, ranked_web = _documents("web", 6, first_score=0.99)
    recalled_academic, ranked_academic = _documents(
        "academic", 1, first_score=0.20
    )
    recalled_patent, ranked_patent = _documents("patent", 1, first_score=0.10)
    recalled_legal, ranked_legal = _documents("legal", 1, first_score=0.15)
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="mixed query",
            normalized_query="mixed query",
            top_k=limit,
        ),
        search_query="mixed query",
        academic_query="academic mixed query",
        active_provider_names=("web-source",),
        do_academic=True,
        do_patent=True,
        do_legal=True,
    )
    options = resolve_ranking_options(
        default_profile="quality",
        default_threshold=0.3,
        default_threshold_mode="off",
    )
    outcome = DiscoveryOutcome(
        query_time=SystemClock().now(),
        planned=planned,
        recalled=RecallOutcome(
            web=recalled_web,
            academic=recalled_academic,
            patent=recalled_patent,
            legal=recalled_legal,
            planned_sources=(
                "web-source", "academic-source", "patent-source", "legal-source"
            ),
            candidate_budget=9,
        ),
        ranked=RankingOutcome(
            options=options,
            reranker="test",
            web=ranked_web,
            academic=ranked_academic,
            patent=ranked_patent,
            legal=ranked_legal,
        ),
    )

    class Discovery:
        def execute(self, command):
            return outcome

    class Trust:
        def annotate(self, **kwargs):
            return TrustOutcome(tuple(kwargs["evidence"]), None)

    class SeedStore:
        def save(self, snapshot, *, ttl_seconds):
            now = SystemClock().now()
            return SearchSeed(
                search_id="srch_test",
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
                evidence_count=len(snapshot.evidence),
                seed_snapshot_hash="sha256:test",
            )

    service = SearchService(
        discovery=Discovery(),
        evidence_assembler=EvidenceAssembler(),
        trust_annotator=Trust(),
        answerability=AnswerabilityPolicy(),
        seed_store=SeedStore(),
        clock=SystemClock(),
        deadline_ms=30000,
        seed_ttl_seconds=60,
    )
    return service.execute(SearchCommand(
        "mixed query",
        limit=limit,
        source_types=("web", "academic", "patent", "legal"),
    ))


def test_explicit_mixed_sources_reserve_coverage_before_global_fill():
    response = _execute(limit=6)

    assert response.result_set.counts_by_type == {
        "web": 3,
        "academic": 1,
        "patent": 1,
        "legal": 1,
    }
    assert response.result_set.counts_by_stage.model_dump() == {
        "recalled": {"web": 6, "academic": 1, "patent": 1, "legal": 1},
        "ranked": {"web": 6, "academic": 1, "patent": 1, "legal": 1},
        "assembled": {"web": 6, "academic": 1, "patent": 1, "legal": 1},
        "selected": {"web": 3, "academic": 1, "patent": 1, "legal": 1},
    }
    assert [item.type for item in response.evidence] == [
        "web", "web", "web", "academic", "legal", "patent"
    ]
    assert response.retrieval_assessment.status == "usable"
    assert response.retrieval_assessment.gaps == []


def test_limit_smaller_than_available_source_types_reports_dropped_coverage():
    response = _execute(limit=2)

    assert response.result_set.counts_by_type == {"web": 2}
    assert response.result_set.counts_by_stage.selected.model_dump() == {
        "web": 2,
        "academic": 0,
        "patent": 0,
        "legal": 0,
    }
    dropped = [
        gap for gap in response.retrieval_assessment.gaps
        if gap.code == "SOURCE_TYPE_DROPPED_BY_LIMIT"
    ]
    assert [gap.type for gap in dropped] == ["academic", "patent", "legal"]
