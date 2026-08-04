"""Structured coverage evaluation and information-gain accounting."""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Sequence

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
    if item.type == "academic":
        return "academic:" + str(
            item.citation.doi or item.citation.work_id or item.result_id
        )
    if item.type == "patent" and item.patent is not None:
        return "patent:" + str(
            item.patent.family_id
            or item.patent.publication_number
            or item.result_id
        )
    if item.provenance is not None:
        return "web:" + str(
            item.provenance.ownership_group
            or item.provenance.canonical_url
            or item.provenance.document_id
            or item.result_id
        )
    return item.result_id


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
                    followup_queries=list(assessment.followup_queries),
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
