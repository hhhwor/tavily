"""Deterministic M3 dossier structure built from verified Research state."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from src.domain.research import (
    ResearchConflict,
    ResearchDossier,
    ResearchFinding,
    ResearchLimitation,
    ResearchMethods,
    ResearchStatement,
    ResearchSummary,
    ResolvedResearch,
)
from src.domain.trust import ClaimAssessment


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(
        "|".join(parts).encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}_{digest}"


_HEADLINES = {
    "sufficient": "现有合格证据充分支持研究结论。",
    "sufficient_with_limitations": "现有证据支持主要结论，但仍存在明确限制。",
    "insufficient": "现有合格证据不足以形成充分结论。",
    "conflicted": "现有合格证据存在未消解冲突。",
    "needs_expert_review": "现有证据需要领域专家进一步审查。",
}


@dataclass(frozen=True, slots=True)
class DossierDecision:
    kind: str
    statement_status: str
    disclose_conflict: bool
    disclose_gap: bool


def dossier_decision(
    assessment_status: str,
    *,
    has_qualified_support: bool,
) -> DossierDecision:
    """Production policy exercised by the offline M3 quality gate."""
    if assessment_status == "supported" and has_qualified_support:
        return DossierDecision("factual", "supported", False, False)
    if assessment_status == "conflicted":
        return DossierDecision("analysis", "conflicted", True, False)
    return DossierDecision("limitation", "insufficient", False, True)


class StructuredDossierBuilder:
    """Make the structured dossier the source of truth for synthesis."""

    version = "structured-dossier.v1"

    def build(
        self,
        dossier: ResearchDossier,
        *,
        resolved: ResolvedResearch,
        counterevidence_claim_refs: Sequence[str],
        evidence_set_revision: int,
        stop_reason: str,
    ) -> ResearchDossier:
        findings = [
            self._finding(item) for item in (
                finding.assessment for finding in dossier.findings
            )
        ]
        statements = [self._statement(finding) for finding in findings]
        conflicts = [
            self._conflict(finding)
            for finding in findings
            if dossier_decision(
                finding.assessment.status,
                has_qualified_support=bool(
                    finding.qualified_relation_refs
                ),
            ).disclose_conflict
        ]
        limitations = self._limitations(dossier)
        summary = ResearchSummary(
            status=dossier.assessment.overall,
            headline=_HEADLINES[dossier.assessment.overall],
            statement_refs=[item.id for item in statements],
            key_finding_refs=[
                item.id for item in findings
                if item.claim.importance == "key"
            ],
        )
        methods = ResearchMethods(
            profile=resolved.profile,
            policy_id=resolved.policy_id,
            policy_version=resolved.policy_version,
            execution_route=resolved.execution_route,
            rounds_completed=len(dossier.rounds),
            query_count=len(dossier.query_trace),
            source_types=list(resolved.scope.source_types or ()),
            counterevidence_claim_refs=list(dict.fromkeys(
                counterevidence_claim_refs
            )),
            evidence_set_revision=evidence_set_revision,
            stop_reason=stop_reason,
        )
        return dossier.model_copy(update={
            "findings": findings,
            "summary": summary,
            "statements": statements,
            "conflicts": conflicts,
            "limitations_detail": limitations,
            "methods": methods,
        })

    @staticmethod
    def _finding(assessment: ClaimAssessment) -> ResearchFinding:
        qualified_support = list(dict.fromkeys(
            relation.evidence_id
            for relation in assessment.relations
            if relation.qualified and relation.relation == "supports"
        ))
        qualified_conflict = list(dict.fromkeys(
            relation.evidence_id
            for relation in assessment.relations
            if relation.qualified and relation.relation == "contradicts"
        ))
        return ResearchFinding(
            id=_stable_id("finding", assessment.claim.id),
            claim=assessment.claim,
            assessment=assessment,
            qualified_relation_refs=qualified_support,
            conflict_relation_refs=qualified_conflict,
            limitations=list(dict.fromkeys(assessment.gaps)),
        )

    @staticmethod
    def _statement(finding: ResearchFinding) -> ResearchStatement:
        assessment = finding.assessment
        decision = dossier_decision(
            assessment.status,
            has_qualified_support=bool(finding.qualified_relation_refs),
        )
        kind = decision.kind
        status = decision.statement_status
        if kind == "factual":
            text = finding.claim.text
        elif kind == "analysis":
            text = (
                f"关于“{finding.claim.text}”，合格证据同时存在支持与反证。"
            )
        else:
            text = f"尚无足够合格证据支持“{finding.claim.text}”。"
        return ResearchStatement(
            id=_stable_id("statement", finding.id, kind, text),
            text=text,
            kind=kind,
            status=status,
            finding_refs=[finding.id],
        )

    @staticmethod
    def _conflict(finding: ResearchFinding) -> ResearchConflict:
        return ResearchConflict(
            id=_stable_id("conflict", finding.id),
            claim_ref=finding.claim.id,
            finding_refs=[finding.id],
            support_evidence_refs=finding.qualified_relation_refs,
            conflict_evidence_refs=finding.conflict_relation_refs,
            message=(
                f"“{finding.claim.text}”存在合格支持证据和合格反证，"
                "综合阶段不对冲突作静默裁决。"
            ),
            review_required=True,
        )

    @staticmethod
    def _limitations(dossier: ResearchDossier) -> list[ResearchLimitation]:
        limitations: list[ResearchLimitation] = []
        for gap in dossier.coverage.gaps:
            limitations.append(ResearchLimitation(
                id=_stable_id("limitation", "gap", gap.id),
                code=gap.code,
                message=gap.message,
                gap_refs=[gap.id],
                evidence_refs=list(gap.evidence_refs),
            ))
        for value in dossier.boundaries.limitations:
            code = value.split(":", 1)[0]
            limitations.append(ResearchLimitation(
                id=_stable_id("limitation", "boundary", value),
                code=code,
                message=value,
            ))
        return list({item.id: item for item in limitations}.values())
