"""Fail-closed citation coverage audit for synthesized Research statements."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from src.domain.evidence import EvidenceLocator
from src.domain.research import CitationAudit, ResearchDossier


LocatorResolver = Callable[[EvidenceLocator], str | None]


class CitationCoverageAuditor:
    version = "citation-audit.v1"

    def audit(
        self,
        dossier: ResearchDossier,
        *,
        resolve_locator: LocatorResolver,
        audited_at: datetime,
    ) -> CitationAudit:
        finding_by_id = {item.id: item for item in dossier.findings}
        evidence_by_id = dossier.evidence_index
        uncited: list[str] = []
        invalid_findings: list[str] = []
        invalid_evidence: list[str] = []
        invalid_locators: list[str] = []
        unsupported_statements: list[str] = []
        cited_factual = 0
        factual = [
            statement for statement in dossier.statements
            if statement.kind == "factual"
        ]

        def valid_relation(
            finding,
            evidence_ref: str,
            relation_type: str,
        ) -> bool:
            evidence = evidence_by_id.get(evidence_ref)
            relation = next((
                item for item in finding.assessment.relations
                if item.evidence_id == evidence_ref
                and item.qualified
                and item.relation == relation_type
            ), None)
            if evidence is None or relation is None:
                invalid_evidence.append(evidence_ref)
                return False
            locator = relation.locator or evidence.locator
            resolved = resolve_locator(locator) if locator is not None else None
            if (
                not resolved
                or not relation.quote
                or relation.quote not in resolved
            ):
                invalid_locators.append(evidence_ref)
                return False
            return True

        for statement in factual:
            if not statement.finding_refs:
                uncited.append(statement.id)
                continue
            if len(statement.finding_refs) != 1:
                unsupported_statements.append(statement.id)
            statement_valid = True
            for finding_ref in statement.finding_refs:
                finding = finding_by_id.get(finding_ref)
                if finding is None or not finding.qualified_relation_refs:
                    invalid_findings.append(finding_ref)
                    statement_valid = False
                    continue
                if statement.text.strip() != finding.claim.text.strip():
                    unsupported_statements.append(statement.id)
                    statement_valid = False
                for evidence_ref in finding.qualified_relation_refs:
                    if not valid_relation(
                        finding, evidence_ref, "supports"
                    ):
                        statement_valid = False
            if statement_valid:
                cited_factual += 1

        represented_factual_findings = {
            finding_ref
            for statement in factual
            for finding_ref in statement.finding_refs
        }
        missing_supported_findings = [
            finding.id
            for finding in dossier.findings
            if finding.assessment.status == "supported"
            and finding.qualified_relation_refs
            and finding.id not in represented_factual_findings
        ]

        declared_conflict_findings = {
            finding_ref
            for conflict in dossier.conflicts
            for finding_ref in conflict.finding_refs
        }
        for statement in dossier.statements:
            if statement.kind == "factual":
                continue
            for finding_ref in statement.finding_refs:
                if finding_ref not in finding_by_id:
                    invalid_findings.append(finding_ref)
            if statement.kind != "analysis":
                continue
            if not statement.finding_refs:
                uncited.append(statement.id)
                continue
            if statement.status == "conflicted":
                # The conflict-specific loop below validates both sides.
                for finding_ref in statement.finding_refs:
                    if finding_ref not in declared_conflict_findings:
                        invalid_findings.append(finding_ref)
                continue
            for finding_ref in statement.finding_refs:
                finding = finding_by_id.get(finding_ref)
                if finding is None:
                    continue
                relation_refs = (
                    finding.qualified_relation_refs
                    + finding.conflict_relation_refs
                )
                if not relation_refs:
                    invalid_findings.append(finding_ref)
                    continue
                support_refs = set(finding.qualified_relation_refs)
                for evidence_ref in relation_refs:
                    valid_relation(
                        finding,
                        evidence_ref,
                        (
                            "supports"
                            if evidence_ref in support_refs
                            else "contradicts"
                        ),
                    )

        conflict_omissions: list[str] = []
        for conflict in dossier.conflicts:
            represented_findings = {
                finding_ref
                for statement in dossier.statements
                if statement.kind == "analysis"
                and statement.status == "conflicted"
                for finding_ref in statement.finding_refs
            }
            relations_valid = True
            for finding_ref in conflict.finding_refs:
                finding = finding_by_id.get(finding_ref)
                if finding is None:
                    invalid_findings.append(finding_ref)
                    relations_valid = False
                    continue
                for evidence_ref in conflict.support_evidence_refs:
                    relations_valid = valid_relation(
                        finding, evidence_ref, "supports"
                    ) and relations_valid
                for evidence_ref in conflict.conflict_evidence_refs:
                    relations_valid = valid_relation(
                        finding, evidence_ref, "contradicts"
                    ) and relations_valid
            if (
                not conflict.finding_refs
                or not set(conflict.finding_refs) <= represented_findings
                or not conflict.support_evidence_refs
                or not conflict.conflict_evidence_refs
                or not relations_valid
            ):
                conflict_omissions.append(conflict.id)

        factual_count = len(factual)
        coverage_rate = (
            cited_factual / factual_count if factual_count else 1.0
        )
        values = (
            uncited,
            invalid_findings,
            invalid_evidence,
            invalid_locators,
            unsupported_statements,
            missing_supported_findings,
            conflict_omissions,
        )
        failed = any(values)
        return CitationAudit(
            status="failed" if failed else "passed",
            factual_statement_count=factual_count,
            cited_factual_statement_count=cited_factual,
            citation_coverage_rate=coverage_rate,
            uncited_statement_refs=list(dict.fromkeys(uncited)),
            invalid_finding_refs=list(dict.fromkeys(invalid_findings)),
            invalid_evidence_refs=list(dict.fromkeys(invalid_evidence)),
            invalid_locator_refs=list(dict.fromkeys(invalid_locators)),
            unsupported_statement_refs=list(dict.fromkeys(
                unsupported_statements
            )),
            missing_supported_finding_refs=list(dict.fromkeys(
                missing_supported_findings
            )),
            conflict_omission_refs=list(dict.fromkeys(conflict_omissions)),
            audited_at=audited_at,
        )
