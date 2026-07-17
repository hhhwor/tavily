"""Phase 1 陈述拆解、蕴含、一致性和门禁测试。"""
import json

from src.engine import SearchEngine
from src.models import AcademicResult, CandidateClaim
from src.trust import ClaimVerifier, annotate_evidence
from src.trust.claims import decompose_claims
from src.trust.entailment import (
    EntailmentDecision,
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
    engine = object.__new__(SearchEngine)
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
    evidence = engine._build_evidence([], [paper], [])
    annotate_evidence(evidence)
    return evidence[0]


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
            raise RuntimeError("backend down")

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
    assert "ENTAILMENT_BACKEND_FALLBACK" in response.trust_assessment.warnings
    assert response.trust_assessment.model == "rules:v1"


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
            return {"choices": [{"message": {"content": json.dumps([{
                "id": pair_id,
                "relation": "supports",
                "confidence": "high",
                "reason": "原文直接支持",
                "quote": "材料循环寿命达到 1000 次。",
            }], ensure_ascii=False)}}]}

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr("src.trust.entailment.requests.post", fake_post)
    classifier = SiliconFlowEntailmentClassifier(
        "token", "https://example.invalid/v1", "test-model", timeout=3
    )
    decisions = classifier.classify_pairs([(pair_id, claim, evidence)])

    assert calls
    assert decisions[pair_id].relation == "supports"
    assert decisions[pair_id].quote == "材料循环寿命达到 1000 次。"


def test_verify_route_is_exposed_in_openapi():
    from src.api import VerifyRequest, app

    evidence = _academic_evidence(
        "材料循环寿命达到 1000 次。",
        work_id="W6",
        doi="10.1/api",
    )
    request = VerifyRequest(
        query="材料寿命",
        claims=[CandidateClaim(id="c1", text="材料循环寿命达到 1000 次")],
        evidence=[evidence],
        profile="scientific",
    )

    assert request.profile == "scientific"
    assert "/verify" in app.openapi()["paths"]
