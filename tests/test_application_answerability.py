"""Unit tests for the application AnswerabilityPolicy."""
from src.application.answerability import AnswerabilityPolicy
from src.models import (
    Evidence,
    EvidenceAccess,
    EvidenceDiagnostics,
    EvidencePassage,
    SearchFailure,
)


def _evidence(
    identifier: str,
    source_type: str,
    *,
    snippet_type: str = "web_content",
    oa_pdf_url: str | None = None,
    partial: bool = False,
) -> Evidence:
    return Evidence(
        id=identifier,
        result_id=f"result:{identifier}",
        type=source_type,
        passage=EvidencePassage(
            text="evidence text",
            snippet_type=snippet_type,
        ),
        access=EvidenceAccess(oa_pdf_url=oa_pdf_url),
        diagnostics=EvidenceDiagnostics(partial=partial),
    )


def test_empty_evidence_reports_blocking_and_expected_source_gaps_in_order():
    failures = [
        SearchFailure(
            stage="provider_search",
            source="openalex_local",
            type="academic",
            code="PROVIDER_SEARCH_FAILED",
            message="boom",
        )
    ]

    answerability = AnswerabilityPolicy().evaluate(
        [],
        failures,
        expected_web=True,
        expected_academic=True,
        expected_patent=True,
        include_pdf_text=False,
    )

    assert answerability.status == "not_answerable"
    assert answerability.confidence == "none"
    assert [gap.code for gap in answerability.gaps] == [
        "NO_EVIDENCE",
        "PARTIAL_FAILURE",
        "NO_WEB_EVIDENCE",
        "NO_ACADEMIC_EVIDENCE",
        "NO_PATENT_EVIDENCE",
    ]
    assert answerability.gaps[1].message == (
        "1 个检索子任务失败; 详见 failures[]。"
    )


def test_information_only_gaps_do_not_make_evidence_partial():
    evidence = [
        _evidence("web-1", "web", partial=True),
        _evidence("paper-1", "academic", snippet_type="pdf_text"),
    ]

    answerability = AnswerabilityPolicy().evaluate(
        evidence,
        [],
        expected_web=True,
        expected_academic=True,
        expected_patent=False,
        include_pdf_text=True,
    )

    assert answerability.status == "answerable"
    assert answerability.confidence == "medium"
    assert [gap.code for gap in answerability.gaps] == [
        "LOW_EVIDENCE_COUNT",
        "PARTIAL_EVIDENCE",
    ]


def test_missing_required_source_forces_low_confidence_even_with_many_items():
    evidence = [
        _evidence("web-1", "web"),
        _evidence("web-2", "web"),
        _evidence("web-3", "web"),
    ]

    answerability = AnswerabilityPolicy().evaluate(
        evidence,
        [],
        expected_web=True,
        expected_academic=True,
        expected_patent=False,
        include_pdf_text=False,
    )

    assert answerability.status == "partial"
    assert answerability.confidence == "low"
    assert [gap.code for gap in answerability.gaps] == [
        "NO_ACADEMIC_EVIDENCE"
    ]


def test_pdf_gap_is_counted_only_when_pdf_text_was_requested():
    evidence = [
        _evidence(
            "paper-1",
            "academic",
            snippet_type="abstract",
            oa_pdf_url="https://example.com/one.pdf",
        ),
        _evidence(
            "paper-2",
            "academic",
            snippet_type="pdf_text",
            oa_pdf_url="https://example.com/two.pdf",
        ),
        _evidence("paper-3", "academic", snippet_type="abstract"),
    ]
    policy = AnswerabilityPolicy()

    requested = policy.evaluate(
        evidence,
        [],
        expected_web=False,
        expected_academic=True,
        expected_patent=False,
        include_pdf_text=True,
    )
    not_requested = policy.evaluate(
        evidence,
        [],
        expected_web=False,
        expected_academic=True,
        expected_patent=False,
        include_pdf_text=False,
    )

    assert requested.status == "partial"
    assert requested.confidence == "medium"
    assert [gap.code for gap in requested.gaps] == ["PDF_TEXT_UNAVAILABLE"]
    assert requested.gaps[0].message == (
        "1 条论文证据只有摘要或元数据,未拿到 PDF 正文。"
    )
    assert not_requested.status == "answerable"
    assert not_requested.confidence == "high"
    assert not_requested.gaps == []


def test_failure_with_complete_coverage_is_partial_medium_confidence():
    evidence = [
        _evidence("web", "web"),
        _evidence("paper", "academic", snippet_type="abstract"),
        _evidence("patent", "patent", snippet_type="patent_abstract"),
    ]
    failure = SearchFailure(
        stage="rerank",
        source="web_reranker",
        type="web",
        code="RERANK_FAILED",
        message="fallback used",
    )

    answerability = AnswerabilityPolicy().evaluate(
        evidence,
        [failure],
        expected_web=True,
        expected_academic=True,
        expected_patent=True,
        include_pdf_text=False,
    )

    assert answerability.status == "partial"
    assert answerability.confidence == "medium"
    assert [gap.code for gap in answerability.gaps] == ["PARTIAL_FAILURE"]
