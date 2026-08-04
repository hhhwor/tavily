"""Structured coverage evaluation and information-gain accounting."""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Sequence

from src.application.document_identity import independent_work_id, normalize_doi
from src.domain.evidence import Evidence
from src.domain.research import (
    CoverageGain,
    CoverageGap,
    CoverageItem,
    CoverageTarget,
    ObjectivePlan,
    ResearchCoverage,
)
from src.domain.trust import ClaimAssessment


def evidence_identity(item: Evidence) -> str:
    """Compatibility wrapper for the M2 independent-work identity."""
    return independent_work_id(item)


def _stable_gap_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"gap_{digest}"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return date.fromisoformat(normalized[:10])
        except ValueError:
            return None


def _evidence_date(item: Evidence, basis: str) -> date | None:
    if basis == "updated":
        value = item.updated_date or (
            item.provenance.updated_at if item.provenance else None
        )
    elif basis == "filing":
        value = item.patent.application_date if item.patent else None
    elif basis == "publication":
        value = (
            item.patent.publication_date
            if item.patent and item.patent.publication_date
            else item.published_date
        )
    else:
        value = item.published_date or (
            item.provenance.published_at if item.provenance else None
        )
    return _parse_date(value)


class CoverageEvaluator:
    def evaluate(
        self,
        plan: ObjectivePlan,
        evidence: Sequence[Evidence],
        assessments: Sequence[ClaimAssessment],
    ) -> ResearchCoverage:
        assessment_by_claim = {
            item.claim.id: item for item in assessments
        }
        matrix = [
            self._evaluate_target(
                target,
                evidence,
                assessment_by_claim,
            )
            for target in plan.coverage_targets
        ]
        gaps: list[CoverageGap] = []
        for assessment in assessments:
            relation_refs = list(dict.fromkeys(
                assessment.support_refs
                + assessment.conflict_refs
                + assessment.mention_refs
            ))
            for code in assessment.gaps:
                gaps.append(CoverageGap(
                    id=_stable_gap_id(
                        f"claim:{assessment.claim.id}:{code}"
                    ),
                    code=code,
                    severity=(
                        "blocking"
                        if assessment.claim.importance == "key"
                        else "warning"
                    ),
                    message=f"{assessment.claim.id}: {code}",
                    suggested_action=(
                        assessment.followup_queries[0]
                        if assessment.followup_queries
                        else "补充可定位的一手证据"
                    ),
                    dimension="claim",
                    value=assessment.claim.id,
                    claim_ref=assessment.claim.id,
                    evidence_refs=relation_refs,
                    followup_queries=list(assessment.followup_queries),
                ))
        for item in evidence:
            failure_code = item.diagnostics.failure_code
            if not failure_code:
                continue
            retryable = failure_code not in {
                "WORK_ID_MISSING",
                "PDF_URL_MISSING",
                "PDF_CURSOR_INVALID",
                "PDF_CURSOR_LOOP",
                "PDF_BYTE_LIMIT_EXCEEDED",
                "PDF_CHUNK_LIMIT_EXCEEDED",
            }
            gaps.append(CoverageGap(
                id=_stable_gap_id(
                    f"document:{item.id}:{failure_code}"
                ),
                code=failure_code,
                severity="warning",
                message=(
                    f"原文读取失败 {item.id}: {failure_code}"
                ),
                retryable=retryable,
                suggested_action="重新检索可读取的原文版本",
                dimension="document",
                value=item.id,
                evidence_refs=[item.id],
            ))
        if plan.profile == "prior_art_landscape":
            publications = {
                item.patent.publication_number.casefold()
                for item in evidence
                if item.patent is not None
                and item.patent.publication_number
            }
            academic_dois = {
                normalize_doi(item.citation.doi)
                for item in evidence
                if item.type == "academic" and item.citation.doi
            }
            for item in evidence:
                if (
                    item.patent is None
                    or item.quality is None
                    or not item.quality.is_original
                ):
                    continue
                family_remaining = [
                    publication
                    for publication in item.patent.family_members
                    if publication.casefold() not in publications
                ]
                if family_remaining:
                    gaps.append(CoverageGap(
                        id=_stable_gap_id(
                            f"family:{item.patent.family_id}:"
                            + "|".join(sorted(family_remaining))
                        ),
                        code="PATENT_FAMILY_NOT_READ",
                        severity="blocking",
                        message=(
                            f"专利族仍有 {len(family_remaining)} 个成员未深读"
                        ),
                        suggested_action="深读同族专利并比较权利要求差异",
                        dimension="patent_family",
                        value=item.patent.family_id or item.result_id,
                        evidence_refs=[item.id],
                    ))
                citation_remaining = [
                    publication
                    for publication in item.patent.patent_citations
                    if publication.casefold() not in publications
                ]
                if citation_remaining:
                    gaps.append(CoverageGap(
                        id=_stable_gap_id(
                            f"citation:{item.id}:"
                            + "|".join(sorted(citation_remaining))
                        ),
                        code="PATENT_CITATIONS_NOT_READ",
                        severity="blocking",
                        message=(
                            f"仍有 {len(citation_remaining)} 个专利引用未深读"
                        ),
                        suggested_action="深读引用专利以核对现有技术",
                        dimension="patent_citation",
                        value=item.id,
                        evidence_refs=[item.id],
                    ))
                npl_remaining = [
                    citation
                    for citation in item.patent.npl_citations
                    if normalize_doi(citation)
                    and normalize_doi(citation) not in academic_dois
                ]
                if npl_remaining:
                    gaps.append(CoverageGap(
                        id=_stable_gap_id(
                            f"npl:{item.id}:"
                            + "|".join(sorted(npl_remaining))
                        ),
                        code="PATENT_NPL_CITATIONS_NOT_SEARCHED",
                        severity="blocking",
                        message=(
                            f"仍有 {len(npl_remaining)} 个非专利引用未检索"
                        ),
                        suggested_action="检索并深读非专利引用",
                        dimension="patent_npl_citation",
                        value=item.id,
                        evidence_refs=[item.id],
                        followup_queries=[npl_remaining[0]],
                    ))
        target_by_key = {
            (target.dimension, target.value): target
            for target in plan.coverage_targets
        }
        for item in matrix:
            target = target_by_key[(item.dimension, item.value)]
            if not target.required or item.status == "covered":
                continue
            code = (
                "COVERAGE_PARTIAL"
                if item.status == "partial"
                else "COVERAGE_MISSING"
            )
            gaps.append(CoverageGap(
                id=_stable_gap_id(
                    f"coverage:{item.dimension}:{item.value}:{code}"
                ),
                code=code,
                severity="blocking",
                message=f"未充分覆盖 {item.dimension}: {item.value}",
                suggested_action=f"围绕 {item.value} 执行补充检索",
                dimension=item.dimension,
                value=item.value,
                claim_ref=item.value if item.dimension == "claim" else None,
            ))
        gaps = list({item.id: item for item in gaps}.values())
        target_met = all(
            not target.required
            or any(
                item.dimension == target.dimension
                and item.value == target.value
                and item.status == "covered"
                for item in matrix
            )
            for target in plan.coverage_targets
        ) and not any(item.severity == "blocking" for item in gaps)
        return ResearchCoverage(
            matrix=matrix,
            gaps=gaps,
            target_met=target_met,
        )

    @staticmethod
    def _evaluate_target(
        target: CoverageTarget,
        evidence: Sequence[Evidence],
        assessment_by_claim: dict[str, ClaimAssessment],
    ) -> CoverageItem:
        refs: list[str] = []
        status = "missing"
        if target.dimension == "source_type":
            refs = [item.id for item in evidence if item.type == target.value]
        elif target.dimension == "claim":
            assessment = assessment_by_claim.get(target.value)
            if assessment is not None:
                refs = list(dict.fromkeys(
                    assessment.support_refs
                    + assessment.conflict_refs
                    + assessment.mention_refs
                ))
                if assessment.status == "supported":
                    status = "covered"
                elif assessment.status in {
                    "conflicted", "inference", "needs_expert_review"
                }:
                    status = "partial"
        elif target.dimension == "required_feature":
            needle = target.value.casefold()
            refs = [
                item.id for item in evidence
                if needle in f"{item.title} {item.passage.text}".casefold()
            ]
        elif target.dimension == "language":
            refs = [
                item.id for item in evidence
                if (item.language or "").casefold() == target.value.casefold()
            ]
        elif target.dimension == "jurisdiction":
            refs = [
                item.id for item in evidence
                if item.patent is not None
                and item.patent.country.upper() == target.value.upper()
            ]
        elif target.dimension == "classification":
            required = target.value.upper()
            refs = [
                item.id for item in evidence
                if item.patent is not None
                and any(
                    actual.upper() == required
                    or actual.upper().startswith(required)
                    for actual in (
                        item.patent.ipc_main,
                        item.patent.cpc_main,
                    )
                    if actual
                )
            ]
        elif target.dimension == "license":
            refs = [
                item.id for item in evidence
                if (
                    item.access.license
                    or (item.provenance.license if item.provenance else None)
                    or ""
                ).casefold() == target.value.casefold()
            ]
        elif target.dimension == "time":
            basis, from_value, to_value = (target.value.split(":", 2) + ["", ""])[:3]
            from_date = _parse_date(from_value)
            to_date = _parse_date(to_value)
            refs = []
            for item in evidence:
                observed = _evidence_date(item, basis)
                if observed is None:
                    continue
                if from_date and observed < from_date:
                    continue
                if to_date and observed > to_date:
                    continue
                refs.append(item.id)
        if refs and status == "missing":
            status = "covered"
        return CoverageItem(
            dimension=target.dimension,
            value=target.value,
            status=status,
            evidence_refs=list(dict.fromkeys(refs)),
        )

    @staticmethod
    def measure_gain(
        previous_coverage: ResearchCoverage,
        current_coverage: ResearchCoverage,
        previous_evidence: Sequence[Evidence],
        current_evidence: Sequence[Evidence],
        previous_assessments: Sequence[ClaimAssessment] = (),
        current_assessments: Sequence[ClaimAssessment] = (),
    ) -> CoverageGain:
        previous_ids = {evidence_identity(item) for item in previous_evidence}
        current_ids = {evidence_identity(item) for item in current_evidence}
        previous_status = {
            (item.dimension, item.value): item.status
            for item in previous_coverage.matrix
        }
        rank = {
            "not_applicable": 0,
            "missing": 0,
            "partial": 1,
            "covered": 2,
        }
        improved = [
            f"{item.dimension}:{item.value}"
            for item in current_coverage.matrix
            if rank[item.status] > rank.get(
                previous_status.get((item.dimension, item.value), "missing"),
                0,
            )
        ]
        previous_conflicts = sum(
            item.status == "conflicted" for item in previous_assessments
        )
        current_conflicts = sum(
            item.status == "conflicted" for item in current_assessments
        )
        previous_locators = sum(
            bool(item.quality and item.quality.has_stable_locator)
            for item in previous_evidence
        )
        current_locators = sum(
            bool(item.quality and item.quality.has_stable_locator)
            for item in current_evidence
        )
        new_evidence = len(current_ids - previous_ids)
        new_conflicts = max(0, current_conflicts - previous_conflicts)
        locator_upgrades = max(0, current_locators - previous_locators)
        score = new_evidence + len(improved) + new_conflicts + locator_upgrades
        return CoverageGain(
            new_independent_evidence=new_evidence,
            newly_improved_targets=improved,
            new_conflicts=new_conflicts,
            locator_upgrades=locator_upgrades,
            score=score,
        )
