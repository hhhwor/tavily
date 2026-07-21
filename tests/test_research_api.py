from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.application.ports.search_seed import (
    SearchSeedIntegrityError,
    search_seed_snapshot_hash_matches,
)
from src.application.commands import ResearchCommand
from src.application.research_service import ResearchService
from src.api import create_app
from src.bootstrap import build_container
from src.config import Settings
from src.domain.evidence import AnswerabilityGap
from src.domain.research import ResearchScope
from src.domain.search_api import (
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchQuery,
    SearchSeedSnapshot,
)
from src.domain.trust import CandidateClaim, ClaimAssessment


def _settings(path: str) -> Settings:
    return Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
        mcp_mode="false",
        state_db_path=path,
        research_max_workers=1,
    )


def _seed_snapshot(**updates) -> SearchSeedSnapshot:
    snapshot = SearchSeedSnapshot(
        query=SearchQuery(
            original="query",
            effective="query",
            filters_requested=RequestedFilters(),
        ),
        evidence=[],
        retrieval_assessment=RetrievalAssessment(),
        retrieval_boundary=RetrievalBoundary(
            query_time=datetime.now(timezone.utc),
            deadline_ms=30_000,
        ),
    )
    return snapshot.model_copy(update=updates)


def test_research_scope_prefers_requested_then_planned_sources_and_supports_old_seeds():
    requested = _seed_snapshot(
        requested_source_types=["web", "academic", "patent"],
        planned_source_types=["web"],
    )
    scope = ResearchService._resolve_scope(
        ResearchCommand(search_id="srch_requested"),
        SimpleNamespace(snapshot=requested),
    )
    assert scope.source_types == ["web", "academic", "patent"]

    planned = _seed_snapshot(planned_source_types=["academic", "patent"])
    scope = ResearchService._resolve_scope(
        ResearchCommand(search_id="srch_planned"),
        SimpleNamespace(snapshot=planned),
    )
    assert scope.source_types == ["academic", "patent"]

    legacy = _seed_snapshot(
        retrieval_assessment=RetrievalAssessment(gaps=[
            AnswerabilityGap(
                code="NO_ACADEMIC_EVIDENCE",
                message="missing academic",
                type="academic",
            ),
            AnswerabilityGap(
                code="NO_PATENT_EVIDENCE",
                message="missing patent",
                type="patent",
            ),
        ]),
    )
    scope = ResearchService._resolve_scope(
        ResearchCommand(search_id="srch_legacy"),
        SimpleNamespace(snapshot=legacy),
    )
    assert scope.source_types == ["academic", "patent"]

    explicit = ResearchScope(source_types=["web"])
    scope = ResearchService._resolve_scope(
        ResearchCommand(search_id="srch_explicit", scope=explicit),
        SimpleNamespace(snapshot=requested),
    )
    assert scope is explicit


def test_legacy_seed_hash_is_accepted_only_when_source_intent_fields_were_absent():
    payload = _seed_snapshot().model_dump(mode="json")
    payload.pop("requested_source_types")
    payload.pop("planned_source_types")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    legacy = SearchSeedSnapshot.model_validate(payload)
    assert search_seed_snapshot_hash_matches(legacy, legacy_hash) is True

    payload["requested_source_types"] = ["web"]
    tampered = SearchSeedSnapshot.model_validate(payload)
    assert search_seed_snapshot_hash_matches(tampered, legacy_hash) is False


def test_counterevidence_gap_is_removed_only_for_searched_claims():
    assessments = [
        ClaimAssessment(
            claim=CandidateClaim(id="claim_1", text="claim one"),
            gaps=["NO_SUPPORTING_EVIDENCE", "COUNTEREVIDENCE_NOT_SEARCHED"],
        ),
        ClaimAssessment(
            claim=CandidateClaim(id="claim_2", text="claim two"),
            gaps=["COUNTEREVIDENCE_NOT_SEARCHED"],
        ),
    ]

    updated = ResearchService._apply_counterevidence_status(
        assessments,
        {"claim_1"},
    )

    assert updated[0].counterevidence_searched is True
    assert updated[0].gaps == ["NO_SUPPORTING_EVIDENCE"]
    assert updated[1].counterevidence_searched is False
    assert updated[1].gaps == ["COUNTEREVIDENCE_NOT_SEARCHED"]
    assert assessments[0].counterevidence_searched is False


def test_search_seed_to_research_dossier_lifecycle(tmp_path):
    container = build_container(
        _settings(str(tmp_path / "state.sqlite3")),
        include_mcp=False,
    )
    with TestClient(create_app(container)) as client:
        search = client.post("/search", json={
            "query": "固态电池是否已经解决界面稳定性问题",
            "limit": 5,
            "source_types": ["web"],
        })
        assert search.status_code == 200
        search_body = search.json()
        assert search_body["schema_version"] == "search.v1"
        assert search_body["research_seed"]["search_id"].startswith("srch_")

        request = {
            "search_id": search_body["research_seed"]["search_id"],
            "profile": "technology_validation",
            "depth": "quick",
            "objective": {
                "question": "固态电池是否已经解决界面稳定性问题",
                "claims": [{
                    "text": "固态电池已经解决界面稳定性问题",
                    "importance": "key",
                }],
            },
        }
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "lifecycle-test"},
            json=request,
        )
        assert started.status_code == 202
        research_id = started.json()["research_id"]
        assert started.headers["location"] == f"/research/{research_id}"
        fixed_seed = container.research_store.get_seed(research_id)
        assert fixed_seed.query.original == request["objective"]["question"]
        assert len(fixed_seed.evidence) == search_body["research_seed"]["evidence_count"]
        assert fixed_seed.requested_source_types == ["web"]

        # 同一 key + 同一请求返回同一资源，不重复创建任务。
        repeated = client.post(
            "/research",
            headers={"Idempotency-Key": "lifecycle-test"},
            json=request,
        )
        assert repeated.status_code == 202
        assert repeated.json()["research_id"] == research_id

        # 任务创建后使用独立快照；原始 seed 被清理也不影响幂等重试。
        container.seed_store._connection.execute(  # noqa: SLF001
            "DELETE FROM search_seeds WHERE search_id = ?",
            (request["search_id"],),
        )
        retried_after_seed_cleanup = client.post(
            "/research",
            headers={"Idempotency-Key": "lifecycle-test"},
            json=request,
        )
        assert retried_after_seed_cleanup.status_code == 202
        assert retried_after_seed_cleanup.json()["research_id"] == research_id

        task = started.json()
        for _ in range(100):
            response = client.get(f"/research/{research_id}?detail=full")
            assert response.status_code == 200
            task = response.json()
            if task["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)

        assert task["state"] in {"completed", "partial"}
        assert task["dossier"] is not None
        assert isinstance(task["dossier"]["evidence_index"], dict)
        assert task["stop"]["reason"]
        assert task["dossier"]["assessment"]["overall"] == "insufficient"
        assert "trust_score" not in task["dossier"]["assessment"]
        assert task["progress"]["rounds_completed"] == 1
        assert len(task["dossier"]["query_trace"]) == 2
        finding = task["dossier"]["findings"][0]["assessment"]
        assert finding["counterevidence_searched"] is False
        assert "COUNTEREVIDENCE_NOT_SEARCHED" in finding["gaps"]
        assert task["stop"]["reason"] == "information_gain_saturated"

        assert container.research_store.cancel_requested(research_id) is False
        stale_cancel = client.post(
            f"/research/{research_id}/cancel",
            json={"task_revision": task["task_revision"] - 1},
        )
        assert stale_cancel.status_code == 409
        assert container.research_store.cancel_requested(research_id) is False

        current = client.get(f"/research/{research_id}")
        etag = current.headers["etag"]
        unchanged = client.get(
            f"/research/{research_id}", headers={"If-None-Match": etag}
        )
        assert unchanged.status_code == 304

        conflict = client.post(
            "/research",
            headers={"Idempotency-Key": "lifecycle-test"},
            json={**request, "depth": "standard"},
        )
        assert conflict.status_code == 409


def test_public_surface_has_only_search_and_research_resources(tmp_path):
    container = build_container(
        _settings(str(tmp_path / "state.sqlite3")),
        include_mcp=False,
    )
    application = create_app(container)
    paths = application.openapi()["paths"]
    assert "/search" in paths
    assert "/research" in paths
    assert "/research/{research_id}" in paths
    assert "/research/{research_id}/feedback" in paths
    assert "/research/{research_id}/cancel" in paths
    assert "/verify" not in paths
    assert "/academic/pdf/text/{work_id}" not in paths

    with TestClient(application) as client:
        rejected = client.post("/search", json={
            "query": "test",
            "top_k": 5,
            "trust_mode": "annotate",
            "include_pdf_text": True,
        })
        assert rejected.status_code == 422
        missing_key = client.post("/research", json={"search_id": "srch_unknown"})
        assert missing_key.status_code == 422
        blank_key = client.post(
            "/research",
            headers={"Idempotency-Key": "   "},
            json={"search_id": "srch_unknown"},
        )
        assert blank_key.status_code == 422


def test_search_seed_hash_is_verified_on_read(tmp_path):
    container = build_container(
        _settings(str(tmp_path / "state.sqlite3")),
        include_mcp=False,
    )
    with TestClient(create_app(container)) as client:
        response = client.post("/search", json={
            "query": "seed integrity",
            "source_types": ["web"],
        })
        search_id = response.json()["research_seed"]["search_id"]
        row = container.seed_store._connection.execute(  # noqa: SLF001
            "SELECT payload FROM search_seeds WHERE search_id = ?",
            (search_id,),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["query"]["original"] = "tampered"
        container.seed_store._connection.execute(  # noqa: SLF001
            "UPDATE search_seeds SET payload = ? WHERE search_id = ?",
            (json.dumps(payload, ensure_ascii=False), search_id),
        )

        with pytest.raises(SearchSeedIntegrityError):
            container.seed_store.get(search_id)
