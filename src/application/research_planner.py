"""Deterministic objective planning and gap-to-action routing for Research M1."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

from src.application.research_policy import ResolvedPolicy
from src.domain.documents import DocumentKind
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
                    if value in {"web", "academic", "patent"}
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
    ) -> list[ResearchAction]:
        used_queries = {
            query
            for result in history
            for query in result.actual_queries
        }
        claim_by_id = {claim.id: claim for claim in plan.claims}
        actions: list[ResearchAction] = []
        for gap in coverage.gaps:
            if not gap.retryable:
                continue
            query = self._query_for_gap(plan, gap, claim_by_id)
            if not query:
                continue
            if query in used_queries:
                query = f"{query} {gap.code.lower().replace('_', ' ')}"
            source_types: list[DocumentKind] = []
            if gap.dimension == "source_type" and gap.value in {
                "web", "academic", "patent"
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
