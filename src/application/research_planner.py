"""Deterministic objective planning and gap-to-action routing for Research M1."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

from src.application.research_policy import ResolvedPolicy
from src.application.document_identity import independent_work_id
from src.domain.documents import DocumentKind
from src.domain.evidence import Evidence
from src.domain.research import (
    CoverageGap,
    CoverageTarget,
    InputQuestion,
    ObjectivePlan,
    ResearchAction,
    ResearchCoverage,
    ResearchInputRequest,
    ResolvedResearch,
    RoundResult,
)
from src.domain.trust import CandidateClaim


_SENTENCE_BREAK = re.compile(r"[。；;！？!?\n]+")
_CLAUSE_BREAK = re.compile(
    r"[,，]|"
    r"\s+(?:and|while|whereas)\s+",
    re.IGNORECASE,
)
_LEADING_CONNECTOR = re.compile(r"^(?:以及|并且|同时|而且|且)\s*")
_LEADING_REQUEST = re.compile(
    r"^(?:请|请你)?(?:分析|研究|评估|说明|比较|调查|总结|判断)[:：\s]*"
)


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _claim_fragments(question: str) -> list[str]:
    normalized = _LEADING_REQUEST.sub("", question.strip())
    fragments: list[str] = []
    for sentence in _SENTENCE_BREAK.split(normalized):
        for fragment in _CLAUSE_BREAK.split(sentence):
            value = _LEADING_CONNECTOR.sub(
                "", fragment.strip(" ，,。；;！？!?：:")
            )
            if value:
                fragments.append(value)
    return list(dict.fromkeys(fragments)) or [normalized]


def _candidate_text(fragment: str) -> tuple[str, str]:
    if "是否" in fragment:
        return fragment.replace("是否", "", 1).strip(), "research_hypothesis"
    if re.match(r"^(?:is|are|does|do|did|can|could|has|have)\b", fragment, re.I):
        return fragment, "research_hypothesis"
    return fragment, "research_question"


@dataclass(frozen=True, slots=True)
class FeedbackResolution:
    resolved: ResolvedResearch
    plan: ObjectivePlan


class ResearchPlanner:
    """Build a stable plan and route every retrieval action to a concrete gap."""

    def build(
        self,
        resolved: ResolvedResearch,
        policy: ResolvedPolicy,
    ) -> ObjectivePlan:
        question = resolved.objective.question or ""
        claims = self._claims(resolved)
        targets = self._targets(resolved, claims)
        ambiguities: list[InputQuestion] = []
        if (
            resolved.profile == "prior_art_landscape"
            and not resolved.scope.jurisdictions
        ):
            ambiguities.append(InputQuestion(
                id="jurisdictions",
                field="scope.jurisdictions",
                prompt="请指定本次专利检索需要覆盖的辖区。",
                kind="multi_select",
                options=["CN", "US", "EP", "WO", "JP", "KR"],
            ))
        if policy.required_source_types:
            required = set(policy.required_source_types)
            planned = set(resolved.scope.source_types or ())
            if planned and not required.issubset(planned):
                missing = ", ".join(sorted(required - planned))
                ambiguities.append(InputQuestion(
                    id="required_sources",
                    field="scope.source_types",
                    prompt=f"Policy 还需要来源类型：{missing}。",
                    kind="multi_select",
                    options=sorted(required),
                ))
        return ObjectivePlan(
            question=question,
            profile=resolved.profile,
            claims=claims,
            coverage_targets=targets,
            ambiguities=ambiguities,
        )

    @staticmethod
    def _claims(resolved: ResolvedResearch) -> list[CandidateClaim]:
        if resolved.objective.claims:
            return [
                CandidateClaim(
                    id=f"claim_{index}",
                    text=item.text,
                    importance=item.importance,
                    subject=item.subject,
                    predicate=item.predicate,
                    value=item.value,
                    unit=item.unit,
                    source=item.source,
                )
                for index, item in enumerate(resolved.objective.claims, start=1)
            ]
        claims: list[CandidateClaim] = []
        for index, fragment in enumerate(
            _claim_fragments(resolved.objective.question or ""),
            start=1,
        ):
            text, claim_type = _candidate_text(fragment)
            claims.append(CandidateClaim(
                id=f"claim_{index}",
                text=text,
                claim_type=claim_type,
                importance="key" if index == 1 else "supporting",
                source="agent",
            ))
        return claims

    @staticmethod
    def _targets(
        resolved: ResolvedResearch,
        claims: Sequence[CandidateClaim],
    ) -> list[CoverageTarget]:
        targets: list[CoverageTarget] = []

        def add(dimension: str, value: str, *, required: bool = True) -> None:
            targets.append(CoverageTarget(
                id=_stable_id(f"target_{dimension}", value),
                dimension=dimension,
                value=value,
                required=required,
            ))

        for source_type in resolved.scope.source_types or ():
            add("source_type", source_type)
        for claim in claims:
            add("claim", claim.id, required=claim.importance == "key")
        for feature in resolved.objective.required_features:
            add("required_feature", feature)
        if resolved.scope.time is not None:
            time = resolved.scope.time
            add(
                "time",
                ":".join((
                    time.basis,
                    time.from_date.isoformat() if time.from_date else "",
                    time.to_date.isoformat() if time.to_date else "",
                )),
            )
        for language in resolved.scope.languages:
            add("language", language)
        for jurisdiction in resolved.scope.jurisdictions:
            add("jurisdiction", jurisdiction)
        for classification in resolved.scope.required_classifications:
            add("classification", classification)
        for license_id in resolved.scope.licenses:
            add("license", license_id)
        return targets

    @staticmethod
    def input_request(plan: ObjectivePlan) -> ResearchInputRequest:
        return ResearchInputRequest(
            id=f"input-plan-{plan.revision}",
            code="RESEARCH_PLAN_NEEDS_INPUT",
            message="研究计划包含会改变检索方向的未决条件。",
            questions=[item.prompt for item in plan.ambiguities],
            typed_questions=list(plan.ambiguities),
        )

    def apply_feedback(
        self,
        resolved: ResolvedResearch,
        plan: ObjectivePlan,
        answers: dict[str, str],
    ) -> FeedbackResolution:
        scope = resolved.scope
        remaining: list[InputQuestion] = []
        adjustments = list(resolved.adjustments)
        for question in plan.ambiguities:
            answer = answers.get(question.id) or answers.get(question.field)
            if not answer:
                if question.required:
                    remaining.append(question)
                continue
            values = [
                value.strip()
                for value in re.split(r"[,，;；\s]+", answer)
                if value.strip()
            ]
            if not values:
                if question.required:
                    remaining.append(question)
                continue
            if question.field == "scope.jurisdictions":
                scope = scope.model_copy(update={
                    "jurisdictions": [value.upper() for value in values]
                })
            elif question.field == "scope.source_types":
                accepted = [
                    value for value in values
                    if value in {"web", "academic", "patent", "legal"}
                ]
                if not accepted:
                    remaining.append(question)
                    continue
                scope = scope.model_copy(update={"source_types": accepted or None})
            else:
                adjustments.append(f"{question.field}: {answer}")
        updated_resolved = resolved.model_copy(update={
            "scope": scope,
            "adjustments": adjustments,
        })
        updated_plan = self.build(
            updated_resolved,
            ResolvedPolicy(
                id=updated_resolved.policy_id,
                version=updated_resolved.policy_version,
                allowed_profiles=frozenset({updated_resolved.profile}),
                required_source_types=frozenset(),
                counterevidence_required=False,
                verification_profile="general",
            ),
        ).model_copy(update={
            "revision": plan.revision + 1,
            "ambiguities": remaining,
        })
        return FeedbackResolution(updated_resolved, updated_plan)

    def next_actions(
        self,
        plan: ObjectivePlan,
        coverage: ResearchCoverage,
        *,
        round_number: int,
        history: Sequence[RoundResult] = (),
        max_actions: int = 1,
        evidence: Sequence[Evidence] = (),
        allow_deep_read: bool = True,
    ) -> list[ResearchAction]:
        used_queries = {
            query
            for result in history
            for query in result.actual_queries
        }
        claim_by_id = {claim.id: claim for claim in plan.claims}
        attempted_deep_reads = {
            candidate_id
            for result in history
            for action in result.actions
            if action.kind == "deep_read"
            for candidate_id in action.candidate_ids
        }
        attempted_related_documents = {
            document_id.casefold()
            for result in history
            for action in result.actions
            for document_id in action.related_document_ids
        }
        deep_read_works = {
            value
            for item in evidence
            if item.access.original_status is not None
            for value in (
                independent_work_id(item),
                f"result:{item.result_id}",
            )
        }
        evidence_by_id = {item.id: item for item in evidence}
        patent_publications = {
            item.patent.publication_number.casefold()
            for item in evidence
            if item.patent is not None and item.patent.publication_number
        }
        actions: list[ResearchAction] = []
        ordered_gaps = sorted(
            enumerate(coverage.gaps),
            key=lambda row: (
                {
                    "PATENT_FAMILY_NOT_READ": 0,
                    "PATENT_CITATIONS_NOT_READ": 1,
                    "PATENT_NPL_CITATIONS_NOT_SEARCHED": 2,
                }.get(row[1].code, 3),
                row[0],
            ),
        )
        for _, gap in ordered_gaps:
            if not gap.retryable:
                continue
            if gap.code == "PATENT_NPL_CITATIONS_NOT_SEARCHED":
                query = gap.followup_queries[0] if gap.followup_queries else None
                candidate = next((
                    evidence_by_id[evidence_id]
                    for evidence_id in gap.evidence_refs
                    if evidence_id in evidence_by_id
                ), None)
                if query and query not in used_queries and candidate is not None:
                    identity = "|".join((
                        str(round_number),
                        "citation_expand",
                        gap.id,
                        query,
                    ))
                    actions.append(ResearchAction(
                        id=_stable_id("action", identity),
                        round=round_number,
                        kind="citation_expand",
                        target_gap_refs=[gap.id],
                        source_types=["academic"],
                        query=query,
                        candidate_ids=[candidate.id],
                        expected_gain=[f"npl:{query}"],
                    ))
                    if len(actions) >= max_actions:
                        break
                continue
            relation_action = self._patent_relation_action(
                gap,
                evidence_by_id,
                patent_publications,
                attempted_related_documents,
            ) if allow_deep_read else None
            if relation_action is not None:
                kind, candidate, related_document = relation_action
                identity = "|".join((
                    str(round_number),
                    kind,
                    gap.id,
                    related_document,
                ))
                actions.append(ResearchAction(
                    id=_stable_id("action", identity),
                    round=round_number,
                    kind=kind,
                    target_gap_refs=[gap.id],
                    source_types=["patent"],
                    candidate_ids=[candidate.id],
                    related_document_ids=[related_document],
                    expected_gain=[
                        f"{gap.dimension}:{related_document}"
                    ],
                ))
                if len(actions) >= max_actions:
                    break
                continue
            if gap.code in {
                "PATENT_FAMILY_NOT_READ",
                "PATENT_CITATIONS_NOT_READ",
            }:
                continue
            candidate = self._deep_read_candidate(
                gap,
                evidence_by_id,
                attempted_deep_reads,
                deep_read_works,
            ) if allow_deep_read else None
            if candidate is not None:
                identity = "|".join((
                    str(round_number),
                    "deep_read",
                    gap.id,
                    candidate.id,
                ))
                actions.append(ResearchAction(
                    id=_stable_id("action", identity),
                    round=round_number,
                    kind="deep_read",
                    target_gap_refs=[gap.id],
                    source_types=[candidate.type],
                    candidate_ids=[candidate.id],
                    expected_gain=[
                        f"locator:{candidate.result_id}",
                        f"version:{candidate.result_id}",
                    ],
                ))
                if len(actions) >= max_actions:
                    break
                continue
            query = self._query_for_gap(plan, gap, claim_by_id)
            if not query:
                continue
            if query in used_queries:
                query = f"{query} {gap.code.lower().replace('_', ' ')}"
            source_types: list[DocumentKind] = []
            if gap.dimension == "source_type" and gap.value in {
                "web", "academic", "patent", "legal"
            }:
                source_types = [gap.value]
            kind = (
                "counter_search"
                if "COUNTER" in gap.code
                else "search"
            )
            identity = "|".join((
                str(round_number),
                kind,
                gap.id,
                query,
            ))
            actions.append(ResearchAction(
                id=_stable_id("action", identity),
                round=round_number,
                kind=kind,
                target_gap_refs=[gap.id],
                source_types=source_types,
                query=query,
                expected_gain=[
                    f"{gap.dimension or 'claim'}:{gap.value or gap.code}"
                ],
            ))
            if len(actions) >= max_actions:
                break
        return actions

    @staticmethod
    def _patent_relation_action(
        gap: CoverageGap,
        evidence_by_id: dict[str, Evidence],
        existing_publications: set[str],
        attempted_documents: set[str],
    ) -> tuple[str, Evidence, str] | None:
        if gap.code not in {
            "PATENT_FAMILY_NOT_READ",
            "PATENT_CITATIONS_NOT_READ",
        }:
            return None
        candidate = next((
            evidence_by_id[evidence_id]
            for evidence_id in gap.evidence_refs
            if evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].patent is not None
        ), None)
        if candidate is None or candidate.patent is None:
            return None
        documents = (
            candidate.patent.family_members
            if gap.code == "PATENT_FAMILY_NOT_READ"
            else candidate.patent.patent_citations
        )
        related = next((
            value for value in documents
            if value.casefold() not in existing_publications
            and value.casefold() not in attempted_documents
        ), None)
        if related is None:
            return None
        kind = (
            "family_expand"
            if gap.code == "PATENT_FAMILY_NOT_READ"
            else "citation_expand"
        )
        return kind, candidate, related

    @staticmethod
    def _deep_read_candidate(
        gap: CoverageGap,
        evidence_by_id: dict[str, Evidence],
        attempted: set[str],
        completed_works: set[str],
    ) -> Evidence | None:
        if gap.code not in {
            "NO_CITABLE_SUPPORT",
            "ABSTRACT_ONLY",
            "NO_STABLE_LOCATOR",
            "NO_SUPPORTING_EVIDENCE",
            "PROVIDER_EXTRACT_NOT_ORIGINAL",
            "PATENT_ABSTRACT_ONLY",
            "CLAIM_TEXT_UNAVAILABLE",
        }:
            return None
        for evidence_id in gap.evidence_refs:
            item = evidence_by_id.get(evidence_id)
            if item is None or item.id in attempted:
                continue
            if item.diagnostics.failure_code:
                continue
            if (
                independent_work_id(item) in completed_works
                or f"result:{item.result_id}" in completed_works
            ):
                continue
            readable = (
                item.type == "academic"
                and bool(item.citation.work_id and item.access.oa_pdf_url)
            ) or (
                item.type == "web" and bool(item.url)
            ) or (
                item.type == "patent"
                and item.patent is not None
                and bool(item.patent.publication_number)
            ) or (
                item.type == "legal"
                and item.legal is not None
                and bool(item.passage.text)
            )
            if not readable:
                continue
            if item.access.original_status is not None:
                continue
            return item
        return None

    @staticmethod
    def _query_for_gap(
        plan: ObjectivePlan,
        gap: CoverageGap,
        claim_by_id: dict[str, CandidateClaim],
    ) -> str | None:
        if gap.followup_queries:
            return gap.followup_queries[0]
        if gap.claim_ref and gap.claim_ref in claim_by_id:
            claim = claim_by_id[gap.claim_ref]
            if "COUNTER" in gap.code:
                return f"{claim.text} 反例 争议 limitation counter evidence"
            return claim.text
        if gap.value:
            return f"{plan.question} {gap.value}".strip()
        if gap.suggested_action:
            return f"{plan.question} {gap.suggested_action}".strip()
        return plan.question or None
