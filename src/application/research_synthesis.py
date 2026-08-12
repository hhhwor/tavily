"""Structured synthesis orchestration with fail-closed citation auditing."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from src.application.citation_audit import (
    CitationCoverageAuditor,
    LocatorResolver,
)
from src.application.ports.synthesis import SynthesisGateway
from src.application.research_execution import ExecutionContext
from src.application.research_execution import ResearchCancelledError
from src.application.ports.runtime import DeadlineExceededError
from src.domain.research import (
    ResearchDossier,
    ResearchStatement,
)
from src.domain.synthesis import (
    SynthesisEvidence,
    SynthesisFinding,
    SynthesisRequest,
)


def _statement_id(statement: ResearchStatement) -> str:
    digest = hashlib.sha256(
        "|".join((
            statement.text,
            statement.kind,
            *statement.finding_refs,
        )).encode("utf-8")
    ).hexdigest()[:16]
    return f"statement_{digest}"


@dataclass(frozen=True, slots=True)
class SynthesisOutcome:
    dossier: ResearchDossier
    mode: str
    model: str = ""
    failure_code: str | None = None
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class ResearchSynthesizer:
    version = "research-synthesis.v1"

    def __init__(
        self,
        *,
        auditor: CitationCoverageAuditor,
        gateway: SynthesisGateway | None = None,
    ) -> None:
        self._auditor = auditor
        self._gateway = gateway

    @property
    def external_available(self) -> bool:
        return bool(
            self._gateway is not None and self._gateway.is_external
        )

    def synthesize(
        self,
        dossier: ResearchDossier,
        *,
        question: str,
        profile: str,
        allow_external_models: bool,
        context: ExecutionContext,
        resolve_locator: LocatorResolver,
        now,
    ) -> SynthesisOutcome:
        deterministic = self._audit(
            dossier,
            resolve_locator=resolve_locator,
            now=now,
        )
        gateway = self._gateway
        if (
            not allow_external_models
            or gateway is None
            or not gateway.is_external
        ):
            return SynthesisOutcome(
                dossier=self._with_mode(deterministic, "deterministic"),
                mode="deterministic",
            )

        input_tokens = 0
        output_tokens = 0
        model_requests = 0
        response_model = getattr(gateway, "name", "external")
        try:
            context.checkpoint()
            context.budget.consume("model_requests")
            model_requests = 1
            response = gateway.synthesize(
                self._request(dossier, question=question, profile=profile),
                deadline=context.deadline,
                cancellation=context.cancellation,
            )
            input_tokens = response.input_tokens
            output_tokens = response.output_tokens
            response_model = response.model
            context.budget.consume("model_input_tokens", response.input_tokens)
            context.budget.consume("model_output_tokens", response.output_tokens)
            statements = [
                item.model_copy(update={"id": _statement_id(item)})
                for item in response.draft.statements
            ]
            if dossier.statements and not statements:
                raise ValueError("synthesis model returned no statements")
            summary = dossier.summary
            assert summary is not None
            candidate = dossier.model_copy(update={
                "statements": statements,
                "summary": summary.model_copy(update={
                    "statement_refs": [item.id for item in statements],
                }),
            })
            audited = self._audit(
                candidate,
                resolve_locator=resolve_locator,
                now=now,
            )
            if (
                audited.citation_audit is not None
                and audited.citation_audit.status == "passed"
            ):
                return SynthesisOutcome(
                    dossier=self._with_mode(audited, "model"),
                    mode="model",
                    model=response.model,
                    model_requests=model_requests,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
            return SynthesisOutcome(
                dossier=self._with_mode(deterministic, "model_fallback"),
                mode="model_fallback",
                model=response.model,
                failure_code="SYNTHESIS_CITATION_AUDIT_FAILED",
                model_requests=model_requests,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        except ResearchCancelledError:
            raise
        except DeadlineExceededError:
            return SynthesisOutcome(
                dossier=self._with_mode(deterministic, "model_fallback"),
                mode="model_fallback",
                model=getattr(gateway, "name", "external"),
                failure_code="SYNTHESIS_DEADLINE_EXCEEDED",
                model_requests=model_requests,
            )
        except Exception:
            return SynthesisOutcome(
                dossier=self._with_mode(deterministic, "model_fallback"),
                mode="model_fallback",
                model=response_model,
                failure_code="SYNTHESIS_MODEL_FAILED",
                model_requests=model_requests,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    def audit_dossier(
        self,
        dossier: ResearchDossier,
        *,
        resolve_locator: LocatorResolver,
        now,
    ) -> ResearchDossier:
        """Revalidate persisted synthesis against the current evidence set."""
        return self._audit(
            dossier,
            resolve_locator=resolve_locator,
            now=now,
        )

    def _audit(
        self,
        dossier: ResearchDossier,
        *,
        resolve_locator: LocatorResolver,
        now,
    ) -> ResearchDossier:
        audit = self._auditor.audit(
            dossier,
            resolve_locator=resolve_locator,
            audited_at=now,
        )
        return dossier.model_copy(update={"citation_audit": audit})

    @staticmethod
    def _with_mode(dossier: ResearchDossier, mode: str) -> ResearchDossier:
        methods = dossier.methods
        if methods is None:
            return dossier
        return dossier.model_copy(update={
            "methods": methods.model_copy(update={
                "synthesis_mode": mode,
            }),
        })

    @staticmethod
    def _request(
        dossier: ResearchDossier,
        *,
        question: str,
        profile: str,
    ) -> SynthesisRequest:
        findings: list[SynthesisFinding] = []
        for finding in dossier.findings:
            evidence_rows: list[SynthesisEvidence] = []
            for relation in finding.assessment.relations:
                if (
                    not relation.qualified
                    or relation.relation not in {"supports", "contradicts"}
                    or not relation.quote
                ):
                    continue
                locator = relation.locator
                if locator is None or not locator.version_id:
                    continue
                evidence_rows.append(SynthesisEvidence(
                    evidence_id=relation.evidence_id,
                    relation=relation.relation,
                    quote=relation.quote,
                    document_id=locator.document_id,
                    version_id=locator.version_id,
                ))
                if len(evidence_rows) >= 5:
                    break
            if not evidence_rows:
                continue
            findings.append(SynthesisFinding(
                finding_id=finding.id,
                claim_id=finding.claim.id,
                claim=finding.claim.text,
                status=finding.assessment.status,
                evidence=evidence_rows,
            ))
        summary = dossier.summary
        return SynthesisRequest(
            question=question,
            profile=profile,
            headline=summary.headline if summary is not None else "",
            findings=findings,
            limitation_codes=[
                item.code for item in dossier.limitations_detail
            ],
        )
