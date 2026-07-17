"""Policy for deciding whether assembled evidence can answer a query."""
from __future__ import annotations

from typing import Sequence

from src.domain.evidence import Answerability, AnswerabilityGap, Evidence
from src.domain.failures import SearchFailure


class AnswerabilityPolicy:
    """Evaluate evidence coverage and preserve the legacy gap semantics."""

    @staticmethod
    def _type_counts(evidence: Sequence[Evidence]) -> dict[str, int]:
        counts = {"web": 0, "academic": 0, "patent": 0}
        for item in evidence:
            if item.type in counts:
                counts[item.type] += 1
        return counts

    def evaluate(
        self,
        evidence: Sequence[Evidence],
        failures: Sequence[SearchFailure],
        *,
        expected_web: bool,
        expected_academic: bool,
        expected_patent: bool,
        include_pdf_text: bool,
    ) -> Answerability:
        counts = self._type_counts(evidence)
        gaps: list[AnswerabilityGap] = []

        if failures:
            gaps.append(AnswerabilityGap(
                code="PARTIAL_FAILURE",
                severity="warning",
                message=(
                    f"{len(failures)} 个检索子任务失败; 详见 failures[]。"
                ),
            ))

        expected = [
            ("web", expected_web, "NO_WEB_EVIDENCE", "未返回网页证据。"),
            (
                "academic",
                expected_academic,
                "NO_ACADEMIC_EVIDENCE",
                "查询需要学术证据,但未返回论文证据。",
            ),
            (
                "patent",
                expected_patent,
                "NO_PATENT_EVIDENCE",
                "查询需要专利证据,但未返回专利证据。",
            ),
        ]
        for source_type, needed, code, message in expected:
            if needed and counts[source_type] == 0:
                gaps.append(AnswerabilityGap(
                    code=code,
                    severity="warning",
                    message=message,
                    type=source_type,
                ))

        if not evidence:
            gaps.insert(0, AnswerabilityGap(
                code="NO_EVIDENCE",
                severity="blocking",
                message="没有可用证据,不应直接回答。",
            ))
        elif len(evidence) < 3:
            gaps.append(AnswerabilityGap(
                code="LOW_EVIDENCE_COUNT",
                severity="info",
                message=(
                    f"仅返回 {len(evidence)} 条证据,回答时应降低确定性。"
                ),
            ))

        if include_pdf_text:
            pdf_gap_count = sum(
                1
                for item in evidence
                if item.type == "academic"
                and item.access.oa_pdf_url
                and item.passage.snippet_type != "pdf_text"
            )
            if pdf_gap_count:
                gaps.append(AnswerabilityGap(
                    code="PDF_TEXT_UNAVAILABLE",
                    severity="warning",
                    message=(
                        f"{pdf_gap_count} 条论文证据只有摘要或元数据,"
                        "未拿到 PDF 正文。"
                    ),
                    type="academic",
                ))

        partial_count = sum(
            1 for item in evidence if item.diagnostics.partial
        )
        if partial_count:
            gaps.append(AnswerabilityGap(
                code="PARTIAL_EVIDENCE",
                severity="info",
                message=(
                    f"{partial_count} 条证据被截断或仍有后续内容。"
                ),
            ))

        if not evidence:
            return Answerability(
                status="not_answerable",
                confidence="none",
                gaps=gaps,
            )
        if any(gap.severity in {"blocking", "warning"} for gap in gaps):
            missing_required = any(
                gap.code
                in {
                    "NO_WEB_EVIDENCE",
                    "NO_ACADEMIC_EVIDENCE",
                    "NO_PATENT_EVIDENCE",
                }
                for gap in gaps
            )
            confidence = (
                "low" if len(evidence) < 3 or missing_required else "medium"
            )
            return Answerability(
                status="partial",
                confidence=confidence,
                gaps=gaps,
            )
        confidence = "high" if len(evidence) >= 3 else "medium"
        return Answerability(
            status="answerable",
            confidence=confidence,
            gaps=gaps,
        )
