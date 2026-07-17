"""F06 来源能力、真实检索边界与 Registry 契约。"""
from __future__ import annotations

import json
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.application.outcomes import PlannedQuery
from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.application.recall import RecallCoordinator
from src.application.source_registry import SourceRegistry
from src.application.trust_annotator import TrustAnnotator
from src.config import Settings
from src.models import SearchPlan, SearchResult
from src.providers.baidu import BaiduSearchProvider
from src.providers.base import SearchProvider
from src.providers.openalex import OpenAlexProvider
from src.providers.patent_es import PatentEsProvider
from src.providers.serpapi import SerpApiProvider
from src.providers.tencent import TencentSearchProvider


class _InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class _Source(SearchProvider):
    def __init__(self, source_id: str, kind: str) -> None:
        self.name = f"legacy-name-{source_id}"
        self.descriptor = SourceDescriptor(
            id=source_id,
            kind=kind,  # type: ignore[arg-type]
            capabilities=frozenset({"time_range_filter"}),
            default_snapshot=f"snapshot:{source_id}",
            count_empty_as_used=True,
        )
        self.calls: list[tuple[str, int, str | None]] = []

    def actual_filters(self, request):
        return {
            "time_from": request.time_from.isoformat() if request.time_from else None,
            "time_to": request.time_to.isoformat() if request.time_to else None,
            "language": request.language,
        }

    def search(self, query, top_k=10, recency=None):
        self.calls.append((query, top_k, recency))
        return [
            SearchResult(
                url=f"https://example.test/{self.descriptor.id}",
                title=self.descriptor.id,
                content="content",
                source=self.name,
            )
        ]


def _settings() -> Settings:
    return Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
        per_provider_k=4,
        cache_enabled=False,
        mcp_mode="false",
    )


def test_registry_routes_by_descriptor_kind_and_preserves_actual_boundary():
    web = _Source("custom-web", "web")
    academic = _Source("papers-v2", "academic")
    patent = _Source("inventions-v3", "patent")
    registry = SourceRegistry([web, academic, patent])
    fixed_now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    coordinator = RecallCoordinator(
        _settings(),
        registry,
        None,
        _InlineExecutor(),
        clock=lambda: fixed_now,
    )
    planned = PlannedQuery(
        plan=SearchPlan(
            raw_query="中文 query",
            normalized_query="中文 query",
            recency="month",
            providers=["custom-web"],
            top_k=3,
        ),
        search_query="中文 query",
        academic_query="academic query",
        active_provider_names=("custom-web",),
        do_academic=True,
        do_patent=True,
    )

    outcome = coordinator.recall(planned)

    assert web.calls == [("中文 query", 4, "month")]
    assert academic.calls == [("academic query", 4, "month")]
    assert patent.calls == [("中文 query", 4, "month")]
    assert outcome.planned_sources == (
        "custom-web",
        "papers-v2",
        "inventions-v3",
    )
    assert set(outcome.providers_used) == {
        "custom-web",
        "papers-v2",
        "inventions-v3",
    }
    assert outcome.candidate_budget == 12
    assert {batch.source.id for batch in outcome.batches} == {
        "custom-web",
        "papers-v2",
        "inventions-v3",
    }
    web_batch = next(
        batch for batch in outcome.batches if batch.source.id == "custom-web"
    )
    assert web_batch.actual_query == "中文 query"
    assert web_batch.snapshot == "snapshot:custom-web"
    assert web_batch.limits["requested_candidates"] == 4
    assert outcome.web[0].source == "custom-web"
    attribution = outcome.web[0].attributions[0]
    assert attribution.provider == "custom-web"
    assert attribution.snapshot == "snapshot:custom-web"
    assert attribution.actual_filters["language"] == "zh"
    assert attribution.actual_filters["time_from"].startswith("2026-06-17")

    trust = TrustAnnotator(lambda _source: "fallback-snapshot").annotate(
        mode="annotate",
        query="中文 query",
        planned_sources=outcome.planned_sources,
        evidence=(),
        query_time=fixed_now,
        candidate_budget=outcome.candidate_budget,
        source_snapshots={
            batch.source.id: batch.snapshot for batch in outcome.batches
        },
    )
    assert trust.search_boundary is not None
    assert trust.search_boundary.source_snapshot["custom-web"] == (
        "snapshot:custom-web"
    )


def test_registry_rejects_duplicate_ids_and_exposes_capabilities():
    first = _Source("duplicate", "web")
    second = _Source("duplicate", "academic")

    with pytest.raises(ValueError, match="重复的 source id"):
        SourceRegistry([first, second])

    registry = SourceRegistry([first])
    assert registry.ids("web") == ("duplicate",)
    assert registry.has_kind("web") is True
    assert registry.has_kind("academic") is False
    assert registry.snapshot_for("duplicate") == "snapshot:duplicate"
    assert registry.snapshot_for("missing") == "snapshot-unavailable"


def test_concrete_sources_declare_and_report_provider_specific_filters():
    request = RetrievalRequest(
        query="中文 query",
        candidate_budget=80,
        recency="month",
        time_from=datetime(2026, 6, 17, tzinfo=timezone.utc),
        time_to=datetime(2026, 7, 17, tzinfo=timezone.utc),
        language="zh",
        jurisdiction="CN",
    )
    tencent = TencentSearchProvider(secret_id="id", secret_key="key")
    baidu = BaiduSearchProvider(api_key="key")
    serpapi = SerpApiProvider(api_key="key", gl="cn", hl="zh-cn")
    openalex = OpenAlexProvider(per_page=25)
    patent = PatentEsProvider(base_url="https://patent.test", index="read-v3")

    assert tencent.actual_filters(request) == {
        "FromTime": int(request.time_from.timestamp()),
        "ToTime": int(request.time_to.timestamp()),
    }
    assert baidu.actual_filters(request)["search_recency_filter"] == "month"
    assert baidu.actual_filters(request)["top_k"] == 50
    assert serpapi.actual_filters(request)["tbs"] == "qdr:m"
    assert serpapi.actual_filters(request)["gl"] == "cn"
    assert openalex.actual_filters(request) == {
        "size": 25,
        "year_min": 2026,
        "year_max": 2026,
    }
    assert patent.actual_filters(request) == {
        "index": "read-v3",
        "size": 25,
        "application_date_gte": "2026-06-17",
    }
    assert patent.descriptor.default_snapshot == "index-alias:read-v3"
    assert openalex.descriptor.data_license == "OpenAlex"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Http:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_explicit_time_boundary_is_the_one_sent_to_adapters():
    request = RetrievalRequest(
        query="query",
        candidate_budget=5,
        recency="month",
        time_from=datetime(2025, 12, 15, tzinfo=timezone.utc),
        time_to=datetime(2026, 1, 14, tzinfo=timezone.utc),
    )

    tencent_http = _Http({"Response": {"Pages": []}})
    TencentSearchProvider(
        secret_id="id",
        secret_key="key",
        http_session=tencent_http,
    ).retrieve(request)
    tencent_body = json.loads(tencent_http.calls[0][1]["data"])
    assert tencent_body["FromTime"] == int(request.time_from.timestamp())
    assert tencent_body["ToTime"] == int(request.time_to.timestamp())

    openalex_http = _Http({"results": []})
    OpenAlexProvider(http_session=openalex_http).retrieve(request)
    assert openalex_http.calls[0][1]["json"]["year_min"] == 2026
    assert openalex_http.calls[0][1]["json"]["year_max"] == 2026

    patent_http = _Http({"hits": {"hits": []}})
    PatentEsProvider(
        base_url="https://patent.test",
        http_session=patent_http,
    ).retrieve(request)
    patent_filter = patent_http.calls[0][1]["json"]["query"]["bool"]["filter"]
    assert patent_filter == [{
        "range": {"application_date": {"gte": "2025-12-15"}}
    }]


def test_application_recall_has_no_concrete_provider_dispatch():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "application" / "recall.py"
    ).read_text(encoding="utf-8")
    assert "academic_provider" not in source
    assert "patent_provider" not in source
    assert "OpenAlexProvider" not in source
    assert "PatentEsProvider" not in source
