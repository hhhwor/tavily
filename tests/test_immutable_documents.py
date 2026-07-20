"""F04 不可变文档阶段与无污染契约。"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.application.commands import SearchCommand
from src.application.evidence_assembler import EvidenceAssembler
from src.application.outcomes import PlannedQuery, RecallOutcome
from src.application.ranking_service import RankingService
from src.application.trust_annotator import TrustAnnotator
from src.config import Settings
from src.domain.documents import RankedDocument, RetrievedDocument
from src.models import SearchPlan, SearchResult
from src.pipeline.rerank import NoOpReranker
from src.infrastructure.runtime import SystemClock


class _InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def test_retrieved_document_recursively_freezes_provider_payload():
    result = SearchResult(
        url="https://example.test/item",
        title="snippet",
        snippet="provider snippet",
        content="provider snippet",
        source="serpapi",
        raw={"nested": {"values": [1, 2]}},
    )
    document = RetrievedDocument.from_result(
        result,
        "web",
        provider_rank=2,
        snapshot="provider-snapshot:v1",
        actual_filters={"recency": "month"},
    )

    result.raw["nested"]["values"].append(3)
    assert document.content_kind == "web_snippet"
    assert document.primary_provider_rank == 2
    assert document.raw_payload["nested"]["values"] == (1, 2)
    assert document.attributions[0].snapshot == "provider-snapshot:v1"
    assert document.attributions[0].actual_filters["recency"] == "month"
    with pytest.raises(FrozenInstanceError):
        document.title = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        document.raw_payload["new"] = "value"  # type: ignore[index]

    materialized = document.to_result()
    materialized.title = "local copy"
    assert document.title == "snippet"


def test_ranking_uses_ephemeral_dtos_and_preserves_source_attributions():
    first = RetrievedDocument.from_result(
        SearchResult(
            url="https://example.test/page?utm_source=a",
            title="short",
            content="short",
            source="tencent",
            raw={"provider": {"id": 1}},
        ),
        "web",
        provider_rank=0,
    )
    second = RetrievedDocument.from_result(
        SearchResult(
            url="https://example.test/page",
            title="richer",
            content="richer content",
            source="baidu",
            raw={"provider": {"id": 2}},
        ),
        "web",
        provider_rank=0,
    )
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
        mcp_mode="false",
    )
    service = RankingService(
        settings,
        NoOpReranker(),
        lambda *_: NoOpReranker(),
        _InlineExecutor(),
        clock=SystemClock(),
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="query",
            normalized_query="query",
            providers=["tencent", "baidu"],
            top_k=5,
        ),
        search_query="query",
        academic_query="query",
    )

    outcome = service.rank(
        SearchCommand("query"),
        planned,
        RecallOutcome(web=(first, second)),
    )

    assert len(outcome.web) == 1
    ranked = outcome.web[0]
    assert {item.provider for item in ranked.attributions} == {"tencent", "baidu"}
    assert ranked.to_result().source in {"baidu+tencent", "tencent+baidu"}
    assert "prior" in ranked.features
    assert "_rrf_prior" not in ranked.document.raw_payload
    assert first.raw_payload.to_dict() == {"provider": {"id": 1}}
    assert second.raw_payload.to_dict() == {"provider": {"id": 2}}


def test_evidence_and_trust_do_not_modify_ranked_or_evidence_inputs():
    retrieved = RetrievedDocument.from_result(
        SearchResult(
            url="https://example.test/result",
            title="Result",
            content="evidence text",
            source="web",
            rerank_score=0.8,
        ),
        "web",
    )
    ranked = RankedDocument(retrieved, 0.8, "quality")
    evidence = EvidenceAssembler().assemble([ranked], [], [])
    before = evidence[0].model_dump()

    annotated = TrustAnnotator(lambda _source: "snapshot:v1").annotate(
        mode="annotate",
        query="query",
        planned_sources=("web",),
        evidence=evidence,
        query_time=datetime.now(timezone.utc),
        candidate_budget=10,
    )

    assert evidence[0].model_dump() == before
    assert evidence[0].provenance is None
    assert annotated.evidence[0] is not evidence[0]
    assert annotated.evidence[0].provenance is not None
    assert ranked.document is retrieved
