from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from src.application.ports.search_seed import SearchSeedIntegrityError
from src.api import create_app
from src.bootstrap import build_container
from src.config import Settings


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
