"""Phase 1 陈述拆解、蕴含、一致性和门禁测试。"""
import json

import pytest

from src.application.evidence_assembler import EvidenceAssembler
from src.models import AcademicResult, CandidateClaim
from src.trust import ClaimVerifier, annotate_evidence
from src.trust.claims import decompose_claims
from src.trust.entailment import (
    EntailmentDecision,
    PartialEntailmentFailure,
    RuleEntailmentClassifier,
    SiliconFlowEntailmentClassifier,
)


def _academic_evidence(
    text: str,
    *,
    work_id: str,
    doi: str,
    page_from: int | None = 1,
    abstract_only: bool = False,
):
    paper = AcademicResult(
        url=f"https://doi.org/{doi}",
        title=f"Paper {work_id}",
        content=text if abstract_only else "abstract",
        source="openalex_local",
        work_id=work_id,
        doi=doi,
        venue="Journal",
        pdf_text="" if abstract_only else text,
        pdf_page_from=None if abstract_only else page_from,
        pdf_page_to=None if abstract_only else page_from,
        rerank_score=0.9,
    )
    evidence = EvidenceAssembler().assemble([], [paper], [])
    return annotate_evidence(evidence)[0]


def _verifier(classifier=None) -> ClaimVerifier:
    return ClaimVerifier(
        classifier or RuleEntailmentClassifier(),
        max_claims=20,
        max_evidence_per_claim=5,
    )


def test_citable_academic_fulltext_supports_scientific_claim():
    claim = CandidateClaim(id="c1", text="该实验的循环寿命达到 1000 次")
    evidence = _academic_evidence(
        "实验结果表明，该实验的循环寿命达到 1000 次。",
        work_id="W1",
        doi="10.1/support",
    )

    response = _verifier().verify(
        query="循环寿命",
        claims=[claim],
        evidence=[evidence],
        profile="scientific",
    )

    assessment = response.assessments[0]
    assert assessment.status == "supported"
    assert assessment.confidence == "medium"
    assert assessment.primary_source_count == 1
    assert assessment.independent_support_count == 1
    assert assessment.support_refs == [evidence.id]
    assert assessment.relations[0].qualified is True
    assert "COUNTEREVIDENCE_NOT_SEARCHED" in assessment.gaps
    assert response.trust_assessment.status == "supported"
    assert response.trust_assessment.evidence_coverage_rate == 1.0
    json.dumps(response.model_dump())


def test_abstract_cannot_qualify_even_when_it_contains_exact_claim():
    claim = CandidateClaim(id="c1", text="该实验的循环寿命达到 1000 次")
    evidence = _academic_evidence(
        "该实验的循环寿命达到 1000 次。",
        work_id="W2",
        doi="10.1/abstract",
        abstract_only=True,
    )

    response = _verifier().verify(
        query="循环寿命",
        claims=[claim],
        evidence=[evidence],
        profile="scientific",
    )

    assessment = response.assessments[0]
    assert assessment.relations[0].relation == "supports"
    assert assessment.relations[0].qualified is False
    assert assessment.status == "insufficient"
    assert "NO_CITABLE_SUPPORT" in assessment.gaps
    assert "ABSTRACT_ONLY" in assessment.gaps


def test_numeric_support_and_conflict_are_both_preserved():
    claim = CandidateClaim(
        id="c1",
        text="电池容量为 100 Wh",
        subject="电池容量",
        predicate="为",
        value="100",
        unit="Wh",
    )
    supporting = _academic_evidence(
        "测试结果显示电池容量为 100 Wh。",
        work_id="W-support",
        doi="10.1/value-support",
    )
    conflicting = _academic_evidence(
        "测试结果显示电池容量为 90 Wh。",
        work_id="W-conflict",
        doi="10.1/value-conflict",
    )

    response = _verifier().verify(
        query="电池容量",
        claims=[claim],
        evidence=[supporting, conflicting],
        profile="scientific",
    )

    assessment = response.assessments[0]
    assert assessment.status == "conflicted"
    assert assessment.review_required is True
    assert assessment.support_refs == [supporting.id]
    assert assessment.conflict_refs == [conflicting.id]
    assert "SOURCE_CONFLICT" in assessment.gaps
    conflict_relation = next(r for r in assessment.relations if r.relation == "contradicts")
    number_check = next(c for c in conflict_relation.consistency_checks if c.name == "number")
    assert number_check.status == "fail"
    assert conflict_relation.qualified is True


def test_compound_claims_are_split_and_structured_conservatively():
    claims = decompose_claims([
        CandidateClaim(
            id="c1",
            text="2025年出货量达到 3.7 GWh；该技术尚未商业化",
        )
    ])

    assert [claim.id for claim in claims] == ["c1.1", "c1.2"]
    assert all(claim.parent_id == "c1" for claim in claims)
    assert claims[0].time_scope == "2025年"
    assert claims[0].value == "3.7"
    assert claims[0].unit == "GWh"
    assert claims[1].value is None


def test_no_matched_evidence_returns_insufficient_and_followup():
    claim = CandidateClaim(id="c1", text="完全无关的候选陈述")
    evidence = _academic_evidence(
        "这是一段关于另一主题的内容。",
        work_id="W3",
        doi="10.1/other",
    )

    response = _verifier().verify(
        query="候选陈述",
        claims=[claim],
        evidence=[evidence],
    )

    assessment = response.assessments[0]
    assert assessment.status == "insufficient"
    assert assessment.relations == []
    assert "NO_MATCHED_EVIDENCE" in assessment.gaps
    assert assessment.followup_queries[0].endswith("原文")


def test_entailment_backend_failure_falls_back_to_rules():
    class BrokenClassifier:
        name = "broken"

        def classify_pairs(self, pairs):
            raise RuntimeError(
                "backend down: https://model.test/run?api_key=secret-model-key"
            )

    claim = CandidateClaim(id="c1", text="材料循环寿命达到 1000 次")
    evidence = _academic_evidence(
        "材料循环寿命达到 1000 次。",
        work_id="W4",
        doi="10.1/fallback",
    )

    response = _verifier(BrokenClassifier()).verify(
        query="材料寿命",
        claims=[claim],
        evidence=[evidence],
        profile="scientific",
    )

    assert response.assessments[0].status == "supported"
    assert response.failures[0].code == "ENTAILMENT_BACKEND_FAILED"
    assert "secret-model-key" not in response.failures[0].message
    assert "model.test" not in response.failures[0].message
    assert "ENTAILMENT_BACKEND_FALLBACK" in response.trust_assessment.warnings
    assert response.trust_assessment.model == "rules:v1"


def test_partial_entailment_failure_only_falls_back_failed_pairs():
    first_claim = CandidateClaim(id="c1", text="Alpha catalyst reaches 100 cycles")
    second_claim = CandidateClaim(id="c2", text="Beta electrolyte reaches 200 cycles")
    first_evidence = _academic_evidence(
        "Alpha catalyst reaches 100 cycles.",
        work_id="W-partial-1",
        doi="10.1/partial-1",
    )
    second_evidence = _academic_evidence(
        "Beta electrolyte reaches 200 cycles.",
        work_id="W-partial-2",
        doi="10.1/partial-2",
    )

    class PartialClassifier:
        name = "partial-model"

        def classify_pairs(self, pairs):
            assert len(pairs) == 2
            first_id, _, _ = pairs[0]
            raise PartialEntailmentFailure(
                decisions={
                    first_id: EntailmentDecision(
                        "mentions",
                        "medium",
                        "model decision preserved",
                        "Alpha catalyst reaches 100 cycles.",
                    )
                },
                failed_pairs=pairs[1:],
                failed_batches=1,
                total_batches=1,
                error_codes=("ENTAILMENT_INVALID_RESPONSE",),
                recoverable=True,
            )

    response = ClaimVerifier(
        PartialClassifier(),
        max_claims=20,
        max_evidence_per_claim=1,
    ).verify(
        query="catalyst and electrolyte cycles",
        claims=[first_claim, second_claim],
        evidence=[first_evidence, second_evidence],
        profile="scientific",
    )

    assert response.assessments[0].relations[0].relation == "mentions"
    assert response.assessments[0].relations[0].reason == "model decision preserved"
    assert response.assessments[1].relations[0].relation == "supports"
    assert response.failures[0].code == "ENTAILMENT_BACKEND_FAILED"
    assert "1/2 个 pair" in response.failures[0].message
    assert response.trust_assessment.model == "partial-model+rules:v1"
    assert "ENTAILMENT_BACKEND_PARTIAL_FALLBACK" in (
        response.trust_assessment.warnings
    )


def test_non_literal_model_quote_cannot_qualify_as_support():
    class FabricatingClassifier:
        name = "fabricating"

        def classify_pairs(self, pairs):
            return {
                pair_id: EntailmentDecision(
                    "supports", "high", "模型声称原文支持", "原文中不存在的 1000 次结论"
                )
                for pair_id, _, _ in pairs
            }

    claim = CandidateClaim(id="c1", text="材料循环寿命达到 1000 次")
    evidence = _academic_evidence(
        "正文只说明测试已经完成，没有报告循环寿命。",
        work_id="W-quote",
        doi="10.1/non-literal-quote",
    )

    response = _verifier(FabricatingClassifier()).verify(
        query="材料寿命",
        claims=[claim],
        evidence=[evidence],
        profile="scientific",
    )

    relation = response.assessments[0].relations[0]
    assert relation.relation == "unclear"
    assert relation.qualified is False
    assert relation.quote in evidence.passage.text
    assert response.assessments[0].status == "insufficient"


def test_siliconflow_classifier_validates_structured_labels(monkeypatch):
    claim = CandidateClaim(id="c1", text="材料循环寿命达到 1000 次")
    evidence = _academic_evidence(
        "材料循环寿命达到 1000 次。",
        work_id="W5",
        doi="10.1/model",
    )
    pair_id = f"{claim.id}::{evidence.id}"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "decisions": [{
                    "id": pair_id,
                    "relation": "supports",
                    "confidence": "high",
                    "reason": "原文直接支持",
                    "quote": "材料循环寿命达到 1000 次。",
                }],
            }, ensure_ascii=False)}}]}

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr("src.trust.entailment.requests.post", fake_post)
    classifier = SiliconFlowEntailmentClassifier(
        "token", "https://example.invalid/v1", "Qwen/Qwen3-8B", timeout=3
    )
    decisions = classifier.classify_pairs([(pair_id, claim, evidence)])

    assert calls
    assert decisions[pair_id].relation == "supports"
    assert decisions[pair_id].quote == "材料循环寿命达到 1000 次。"
    request_payload = calls[0][1]["json"]
    assert request_payload["response_format"] == {"type": "json_object"}
    assert request_payload["enable_thinking"] is False
    assert [item["role"] for item in request_payload["messages"]] == [
        "system", "user"
    ]
    assert classifier.runtime_status()["status"] == "available"


def test_siliconflow_classifier_retries_only_missing_pairs(monkeypatch):
    pairs = []
    for index in range(2):
        claim = CandidateClaim(id=f"c{index}", text=f"claim {index}")
        evidence = _academic_evidence(
            f"claim {index}",
            work_id=f"W-retry-{index}",
            doi=f"10.1/retry-{index}",
        )
        pairs.append((f"{claim.id}::{evidence.id}", claim, evidence))

    class Response:
        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(self.rows)}}]}

    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        pair = pairs[len(calls) - 1]
        return Response([{
            "id": pair[0],
            "relation": "supports",
            "confidence": "high",
            "reason": "direct",
            "quote": pair[2].passage.text,
        }])

    monkeypatch.setattr("src.trust.entailment.requests.post", fake_post)
    classifier = SiliconFlowEntailmentClassifier(
        "token", "https://example.invalid/v1", "test-model", timeout=3
    )

    decisions = classifier.classify_pairs(pairs)

    assert set(decisions) == {pairs[0][0], pairs[1][0]}
    assert len(calls) == 2
    first_payload = calls[0]["json"]["messages"][-1]["content"]
    second_payload = calls[1]["json"]["messages"][-1]["content"]
    assert pairs[0][0] in first_payload and pairs[1][0] in first_payload
    assert pairs[0][0] not in second_payload and pairs[1][0] in second_payload


def test_siliconflow_classifier_preserves_successful_batches_on_failure(monkeypatch):
    pairs = []
    for index in range(13):
        claim = CandidateClaim(id=f"c{index}", text=f"claim {index}")
        evidence = _academic_evidence(
            f"claim {index}",
            work_id=f"W-batch-{index}",
            doi=f"10.1/batch-{index}",
        )
        pairs.append((f"{claim.id}::{evidence.id}", claim, evidence))

    class Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            rows = [{
                "id": pair_id,
                "relation": "mentions",
                "confidence": "medium",
                "reason": "model batch succeeded",
                "quote": evidence.passage.text,
            } for pair_id, _, evidence in pairs[:12]]
            return Response(json.dumps(rows))
        return Response("not valid json")

    monkeypatch.setattr("src.trust.entailment.requests.post", fake_post)
    classifier = SiliconFlowEntailmentClassifier(
        "token", "https://example.invalid/v1", "test-model", timeout=3
    )

    with pytest.raises(PartialEntailmentFailure) as caught:
        classifier.classify_pairs(pairs)

    failure = caught.value
    assert set(failure.decisions) == {pair_id for pair_id, _, _ in pairs[:12]}
    assert [pair_id for pair_id, _, _ in failure.failed_pairs] == [pairs[12][0]]
    assert failure.failed_batches == 1
    assert failure.total_batches == 2
    assert failure.error_codes == ("ENTAILMENT_INVALID_RESPONSE",)
    assert len(calls) == 3
    status = classifier.runtime_status()
    assert status["status"] == "degraded"
    assert status["last_failure_codes"] == ["ENTAILMENT_INVALID_RESPONSE"]


def test_verify_is_internal_and_research_routes_are_public():
    from src.api import app

    paths = app.openapi()["paths"]
    assert "/verify" not in paths
    assert "/academic/pdf/text/{work_id}" not in paths
    assert "/research" in paths
    assert "/research/{research_id}" in paths
