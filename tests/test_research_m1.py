from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.application.research_coverage import CoverageEvaluator
from src.application.research_planner import ResearchPlanner
from src.application.research_policy import ResearchPolicyRegistry
from src.api import create_app
from src.bootstrap import build_container
from src.config import Settings
from src.domain.evidence import (
    Evidence,
    EvidenceAccess,
    EvidenceLocator,
    EvidencePassage,
    EvidencePatent,
    EvidenceProvenance,
    EvidenceQuality,
)
from src.domain.research import (
    CandidateClaimInput,
    ResearchBudget,
    ResearchObjective,
    ResearchPrivacy,
    ResearchScope,
    ResearchTimeScope,
    ResolvedResearch,
)
from src.domain.search_api import (
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchQuery,
    SearchSeedSnapshot,
)
from src.domain.trust import ClaimAssessment


def _resolved(
    *,
    question: str = "固态电池是否已经量产，以及有哪些关键限制？",
    profile: str = "technology_validation",
    scope: ResearchScope | None = None,
    objective: ResearchObjective | None = None,
) -> ResolvedResearch:
    return ResolvedResearch(
        objective=objective or ResearchObjective(question=question),
        scope=scope or ResearchScope(source_types=["web", "academic"]),
        profile=profile,
        depth="quick",
        policy_id=(
            "prior-art-evidence.v1"
            if profile == "prior_art_landscape"
            else "technical-evidence.v1"
        ),
        budget=ResearchBudget(
            max_rounds=1,
            max_candidates=30,
            max_deep_reads=2,
            deadline_ms=30_000,
        ),
        privacy=ResearchPrivacy(),
    )


def _patent_evidence() -> Evidence:
    return Evidence(
        id="patent:1",
        result_id="patent:1",
        type="patent",
        title="固态电池界面稳定性专利",
        url="https://example.test/patent/1",
        published_date="2025-01-02",
        language="zh",
        passage=EvidencePassage(text="复合涂层改善固态电池界面稳定性。"),
        access=EvidenceAccess(license="cc-by"),
        provenance=EvidenceProvenance(
            canonical_url="https://example.test/patent/1",
            retrieved_at="2026-08-04T00:00:00Z",
            license="cc-by",
            content_origin="original",
            publisher_type="patent_authority",
        ),
        patent=EvidencePatent(
            publication_number="CN123456A",
            family_id="family-1",
            country="CN",
            application_date="2024-01-01",
            publication_date="2025-01-02",
            ipc_main="H01M10/00",
        ),
        quality=EvidenceQuality(
            level="citable",
            has_stable_locator=True,
            can_support_key_claim=True,
        ),
        locator=EvidenceLocator(
            document_id="CN123456A",
            version_id="CN-A",
            claim_number="1",
        ),
    )


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


def _wait_for_state(
    client: TestClient,
    research_id: str,
    terminal: set[str],
) -> dict:
    task: dict = {}
    for _ in range(200):
        response = client.get(f"/research/{research_id}?detail=full")
        assert response.status_code == 200
        task = response.json()
        if task["state"] in terminal:
            return task
        time.sleep(0.01)
    raise AssertionError(f"research task did not reach {terminal}: {task}")


def test_planner_atomizes_question_and_routes_every_action_to_a_gap():
    resolved = _resolved()
    policy = ResearchPolicyRegistry().resolve(
        resolved.policy_id,
        profile=resolved.profile,
    )
    planner = ResearchPlanner()
    plan = planner.build(resolved, policy)

    assert [claim.text for claim in plan.claims] == [
        "固态电池已经量产",
        "有哪些关键限制",
    ]
    assert all(claim.text != plan.question for claim in plan.claims)

    assessments = [
        ClaimAssessment(
            claim=claim,
            gaps=["NO_SUPPORTING_EVIDENCE"],
            followup_queries=[f"{claim.text} 一手证据"],
        )
        for claim in plan.claims
    ]
    coverage = CoverageEvaluator().evaluate(plan, [], assessments)
    actions = planner.next_actions(
        plan,
        coverage,
        round_number=1,
    )

    assert len(actions) == 1
    assert actions[0].target_gap_refs
    gap_ids = {gap.id for gap in coverage.gaps}
    assert set(actions[0].target_gap_refs).issubset(gap_ids)
    assert actions[0].query == "固态电池已经量产 一手证据"


def test_coverage_evaluator_executes_all_m1_dimensions_and_measures_gain():
    scope = ResearchScope(
        source_types=["patent"],
        time=ResearchTimeScope(
            **{"from": "2024-01-01", "to": "2026-01-01"},
            basis="publication",
        ),
        languages=["zh"],
        jurisdictions=["CN"],
        licenses=["cc-by"],
        required_classifications=["H01M10"],
    )
    objective = ResearchObjective(
        question="复合涂层是否改善固态电池界面稳定性？",
        claims=[CandidateClaimInput(
            text="复合涂层改善固态电池界面稳定性",
        )],
        required_features=["界面稳定性"],
    )
    resolved = _resolved(scope=scope, objective=objective)
    policy = ResearchPolicyRegistry().resolve(
        resolved.policy_id,
        profile=resolved.profile,
    )
    plan = ResearchPlanner().build(resolved, policy)
    evidence = _patent_evidence()
    assessment = ClaimAssessment(
        claim=plan.claims[0],
        status="supported",
        support_refs=[evidence.id],
        counterevidence_searched=True,
    )
    evaluator = CoverageEvaluator()
    empty = evaluator.evaluate(plan, [], [])
    covered = evaluator.evaluate(plan, [evidence], [assessment])
    gain = evaluator.measure_gain(
        empty,
        covered,
        [],
        [evidence],
        [],
        [assessment],
    )

    assert {item.dimension for item in covered.matrix} == {
        "source_type",
        "claim",
        "required_feature",
        "time",
        "language",
        "jurisdiction",
        "classification",
        "license",
    }
    assert covered.target_met is True
    assert not covered.gaps
    assert gain.new_independent_evidence == 1
    assert len(gain.newly_improved_targets) == 8
    assert gain.improved is True


def test_round_checkpoint_is_auditable_and_resume_does_not_duplicate_round(tmp_path):
    container = build_container(
        _settings(str(tmp_path / "state.sqlite3")),
        include_mcp=False,
    )
    with TestClient(create_app(container)) as client:
        search = client.post("/search", json={
            "query": "固态电池是否已经量产，以及有哪些关键限制？",
            "source_types": ["web"],
        })
        assert search.status_code == 200
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m1-checkpoint"},
            json={
                "search_id": search.json()["research_seed"]["search_id"],
                "depth": "quick",
            },
        )
        research_id = started.json()["research_id"]
        task = _wait_for_state(
            client,
            research_id,
            {"completed", "partial", "failed"},
        )

        latest_plan = container.research_store.latest_plan(research_id)
        assert latest_plan is not None
        attempt, plan = latest_plan
        assert len(plan.claims) == 2
        checkpoint = container.research_store.latest_checkpoint(
            research_id,
            attempt=attempt,
        )
        assert checkpoint is not None
        saved_checkpoint, _ = checkpoint
        assert saved_checkpoint.result.actions[0].target_gap_refs
        before_gap_ids = {
            gap.id for gap in saved_checkpoint.result.coverage_before.gaps
        }
        assert set(
            saved_checkpoint.result.actions[0].target_gap_refs
        ).issubset(before_gap_ids)
        assert saved_checkpoint.result.actual_queries
        assert task["progress"]["last_checkpoint_at"] is not None
        assert len(task["dossier"]["rounds"]) == 1

        current = container.research_store.get(research_id)
        restarted = current.model_copy(update={
            "state": "running",
            "phase": "expanding",
            "task_revision": current.task_revision + 1,
            "dossier": None,
            "stop": None,
        })
        container.research_store.save(
            restarted,
            expected_revision=current.task_revision,
        )
        container.engine._research_service.run(research_id)  # noqa: SLF001
        resumed = container.research_store.get(research_id)

        assert resumed.state in {"completed", "partial", "failed"}
        assert resumed.usage.rounds == 1
        assert len(container.research_store.list_rounds(research_id)) == 1


def test_prior_art_ambiguity_enters_needs_input_and_feedback_replans(tmp_path):
    container = build_container(
        _settings(str(tmp_path / "state.sqlite3")),
        include_mcp=False,
    )
    with TestClient(create_app(container)) as client:
        search = client.post("/search", json={
            "query": "固态电池复合涂层现有技术",
            "source_types": ["patent"],
        })
        assert search.status_code == 200
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m1-needs-input"},
            json={
                "search_id": search.json()["research_seed"]["search_id"],
                "profile": "prior_art_landscape",
                "depth": "quick",
            },
        )
        research_id = started.json()["research_id"]
        waiting = _wait_for_state(client, research_id, {"needs_input"})

        assert waiting["stop"]["reason"] == "needs_input"
        questions = waiting["input_request"]["typed_questions"]
        assert questions[0]["id"] == "jurisdictions"
        assert questions[0]["field"] == "scope.jurisdictions"

        feedback = client.post(
            f"/research/{research_id}/feedback",
            json={
                "task_revision": waiting["task_revision"],
                "answers": {"jurisdictions": "CN, WO"},
            },
        )
        assert feedback.status_code == 200
        final = _wait_for_state(
            client,
            research_id,
            {"completed", "partial", "failed"},
        )

        assert final["resolved"]["scope"]["jurisdictions"] == ["CN", "WO"]
        assert final["input_request"] is None
        assert final["dossier"]["plan"]["ambiguities"] == []
        latest_plan = container.research_store.latest_plan(research_id)
        assert latest_plan is not None
        assert latest_plan[0] == 2
        assert latest_plan[1].revision == 2


def test_needs_input_task_can_be_cancelled_with_revision_guard(tmp_path):
    container = build_container(
        _settings(str(tmp_path / "state.sqlite3")),
        include_mcp=False,
    )
    with TestClient(create_app(container)) as client:
        search = client.post("/search", json={
            "query": "固态电池专利现有技术",
            "source_types": ["patent"],
        })
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m1-cancel-needs-input"},
            json={
                "search_id": search.json()["research_seed"]["search_id"],
                "profile": "prior_art_landscape",
                "depth": "quick",
            },
        )
        research_id = started.json()["research_id"]
        waiting = _wait_for_state(client, research_id, {"needs_input"})

        stale = client.post(
            f"/research/{research_id}/cancel",
            json={"task_revision": waiting["task_revision"] - 1},
        )
        assert stale.status_code == 409
        cancelled = client.post(
            f"/research/{research_id}/cancel",
            json={"task_revision": waiting["task_revision"]},
        )

        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        assert cancelled.json()["stop"]["reason"] == "cancelled_by_user"


def test_seed_satisfied_plan_commits_evidence_set_without_expansion_round(tmp_path):
    container = build_container(
        _settings(str(tmp_path / "state.sqlite3")),
        include_mcp=False,
    )
    evidence = _patent_evidence()
    snapshot = SearchSeedSnapshot(
        requested_source_types=["patent"],
        planned_source_types=["patent"],
        query=SearchQuery(
            original="复合涂层改善固态电池界面稳定性",
            effective="复合涂层改善固态电池界面稳定性",
            filters_requested=RequestedFilters(),
        ),
        evidence=[evidence],
        retrieval_assessment=RetrievalAssessment(status="usable"),
        retrieval_boundary=RetrievalBoundary(
            query_time=datetime.now(timezone.utc),
            deadline_ms=30_000,
        ),
    )
    seed = container.seed_store.save(snapshot, ttl_seconds=3600)
    with TestClient(create_app(container)) as client:
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m1-seed-satisfied"},
            json={
                "search_id": seed.search_id,
                "profile": "technology_landscape",
                "depth": "quick",
                "objective": {
                    "question": "复合涂层改善固态电池界面稳定性",
                    "claims": [{
                        "text": "复合涂层改善固态电池界面稳定性",
                    }],
                },
            },
        )
        task = _wait_for_state(
            client,
            started.json()["research_id"],
            {"completed", "partial", "failed"},
        )

        assert task["state"] == "completed"
        assert task["stop"]["reason"] == "objective_satisfied"
        assert task["progress"]["rounds_completed"] == 0
        assert task["evidence_set_revision"] == 1
        assert task["dossier"]["rounds"] == []
        row = container.research_store._connection.execute(  # noqa: SLF001
            """
            SELECT payload FROM research_evidence_sets
            WHERE research_id = ? AND evidence_set_revision = 1
            """,
            (task["research_id"],),
        ).fetchone()
        assert row is not None
