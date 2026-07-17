"""Phase 1 陈述级证据校验编排。"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence

from src.application.ports.entailment import EntailmentClassifier
from src.models import (
    CandidateClaim,
    ClaimAssessment,
    ClaimEvidenceRelation,
    ConsistencyCheck,
    Evidence,
    SearchBoundary,
    SearchFailure,
    TrustAssessment,
    VerifyResponse,
)
from src.domain.errors import public_error_message
from src.trust.annotate import annotate_evidence, build_search_boundary
from src.trust.claims import decompose_claims
from src.trust.entailment import (
    EntailmentDecision,
    EntailmentPair,
    RuleEntailmentClassifier,
    SiliconFlowEntailmentClassifier,
    best_quote,
    normalize_text,
    text_tokens,
)

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_NEGATION = re.compile(r"不|未|无|没有|并非|不能|not\b|no\b|never\b|without\b", re.I)
_PROFILES = {"general", "news", "scientific", "patent", "legal", "financial", "product"}


def _match_score(claim: CandidateClaim, evidence: Evidence) -> float:
    claim_norm = normalize_text(claim.text)
    haystack = f"{evidence.title}\n{evidence.passage.text}"
    evidence_norm = normalize_text(haystack)
    if claim_norm and claim_norm in evidence_norm:
        return 2.0
    claim_tokens = text_tokens(claim.text)
    if not claim_tokens:
        return 0.0
    score = len(claim_tokens & text_tokens(haystack)) / len(claim_tokens)
    for value in (claim.subject, claim.predicate, claim.value, claim.time_scope):
        if value and normalize_text(value) in evidence_norm:
            score += 0.15
    if evidence.quality and evidence.quality.level == "citable":
        score += 0.05
    return score


def _match_evidence(claim: CandidateClaim, evidence: Sequence[Evidence], limit: int) -> List[Evidence]:
    ranked = sorted(
        ((_match_score(claim, item), index, item) for index, item in enumerate(evidence)),
        key=lambda row: (-row[0], row[1]),
    )
    return [item for score, _, item in ranked if score >= 0.15][:limit]


def _check_presence(name: str, expected: Optional[str], quote: str) -> Optional[ConsistencyCheck]:
    if not expected:
        return None
    matched = normalize_text(expected) in normalize_text(quote)
    return ConsistencyCheck(
        name=name,
        status="pass" if matched else "fail",
        claim_value=expected,
        evidence_value=expected if matched else None,
        reason="限定条件在证据片段中出现" if matched else "证据片段未出现该限定条件",
    )


def _consistency_checks(claim: CandidateClaim, evidence: Evidence, quote: str) -> List[ConsistencyCheck]:
    checks: List[ConsistencyCheck] = []
    for name, expected in (("entity", claim.subject), ("predicate", claim.predicate)):
        check = _check_presence(name, expected, quote)
        if check:
            checks.append(check)

    if claim.value:
        expected = claim.value.replace(",", "")
        observed = [value.replace(",", "") for value in _NUMBER.findall(quote)]
        status = "pass" if expected in observed else ("fail" if observed else "unknown")
        checks.append(ConsistencyCheck(
            name="number",
            status=status,
            claim_value=expected,
            evidence_value=", ".join(observed) or None,
            reason="数值一致" if status == "pass" else "数值不一致或证据未给出可比数值",
        ))

    unit_check = _check_presence("unit", claim.unit, quote)
    if unit_check:
        checks.append(unit_check)

    if claim.time_scope:
        expected_years = set(_YEAR.findall(claim.time_scope))
        observed_years = set(_YEAR.findall(quote))
        if not expected_years or expected_years & observed_years:
            status = "pass"
        elif observed_years:
            status = "fail"
        else:
            status = "unknown"
        checks.append(ConsistencyCheck(
            name="date", status=status, claim_value=claim.time_scope,
            evidence_value=", ".join(sorted(observed_years)) or None,
            reason="时间范围一致" if status == "pass" else "时间范围不一致或片段未给出时间",
        ))

    if claim.jurisdiction:
        observed = evidence.patent.country if evidence.patent else None
        if observed:
            status = "pass" if observed.upper() == claim.jurisdiction.upper() else "fail"
        else:
            present = normalize_text(claim.jurisdiction) in normalize_text(quote)
            status = "pass" if present else "unknown"
        checks.append(ConsistencyCheck(
            name="jurisdiction", status=status, claim_value=claim.jurisdiction,
            evidence_value=observed,
            reason="辖区一致" if status == "pass" else "辖区不一致或无法确认",
        ))

    claim_negated = bool(_NEGATION.search(claim.text))
    evidence_negated = bool(_NEGATION.search(quote))
    checks.append(ConsistencyCheck(
        name="negation",
        status="pass" if claim_negated == evidence_negated else "fail",
        claim_value=str(claim_negated).lower(),
        evidence_value=str(evidence_negated).lower(),
        reason="否定极性一致" if claim_negated == evidence_negated else "否定极性不一致",
    ))
    version_id = evidence.locator.version_id if evidence.locator else None
    checks.append(ConsistencyCheck(
        name="version", status="pass" if version_id else "unknown",
        evidence_value=version_id,
        reason="证据版本可定位" if version_id else "证据版本未解析",
    ))
    return checks


def _relation_qualified(
    relation: str,
    evidence: Evidence,
    checks: Sequence[ConsistencyCheck],
) -> bool:
    if relation not in {"supports", "contradicts"}:
        return False
    if not evidence.quality or not evidence.quality.can_support_key_claim:
        return False
    failed = {check.name for check in checks if check.status == "fail"}
    if relation == "supports":
        return not failed
    # 数值或否定不一致可以构成反证；实体、单位、日期、辖区不一致则不是有效反证。
    return not (failed & {"entity", "predicate", "unit", "date", "jurisdiction"})


def _independence_group(evidence: Evidence) -> str:
    if evidence.provenance:
        if evidence.provenance.syndication_group:
            return evidence.provenance.syndication_group
        if evidence.type == "academic":
            return f"academic:{evidence.provenance.document_id}"
        if evidence.type == "patent":
            family_id = evidence.patent.family_id if evidence.patent else ""
            return f"patent-family:{family_id or evidence.provenance.document_id}"
        return (
            evidence.provenance.ownership_group
            or evidence.provenance.publisher_id
            or evidence.provenance.canonical_url
        )
    return evidence.result_id


def _is_primary(evidence: Evidence, profile: str) -> bool:
    if not evidence.provenance:
        return False
    origin = evidence.provenance.content_origin
    publisher_type = evidence.provenance.publisher_type
    if evidence.type == "academic" and origin == "fulltext" and profile in {"general", "scientific"}:
        return True
    if evidence.type == "patent" and evidence.locator and evidence.locator.claim_number:
        return True
    return publisher_type in {"government", "regulator", "patent_authority"} and origin in {
        "original", "fulltext",
    }


def _unique(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(item for item in items if item))


class ClaimVerifier:
    def __init__(
        self,
        classifier: EntailmentClassifier,
        *,
        max_claims: int = 20,
        max_evidence_per_claim: int = 5,
        monotonic=time.monotonic,
    ) -> None:
        self.classifier = classifier
        self.rule_fallback = RuleEntailmentClassifier()
        self.max_claims = max(1, max_claims)
        self.max_evidence_per_claim = max(1, max_evidence_per_claim)
        self._monotonic = monotonic

    def verify(
        self,
        *,
        query: str,
        claims: Sequence[CandidateClaim],
        evidence: Sequence[Evidence],
        profile: str = "general",
        search_boundary: Optional[SearchBoundary] = None,
    ) -> VerifyResponse:
        started = self._monotonic()
        profile = (profile or "general").strip().lower()
        if profile not in _PROFILES:
            raise ValueError(f"不支持的 verification profile: {profile}")
        atomic_claims = decompose_claims(claims, max_claims=self.max_claims)
        evidence_items = [item.model_copy(deep=True) for item in evidence]
        for index, item in enumerate(evidence_items):
            if item.provenance is None or item.locator is None or item.quality is None:
                evidence_items[index] = annotate_evidence([item])[0]

        if search_boundary is None:
            sources = [item.source for item in evidence_items]
            search_boundary = build_search_boundary(
                query=query,
                source_names=sources,
                evidence=evidence_items,
                source_snapshot={name: "client-supplied:unspecified" for name in sources},
                max_candidates=len(evidence_items),
            )

        pair_rows: List[EntailmentPair] = []
        matched_by_claim: Dict[str, List[Evidence]] = {}
        for claim in atomic_claims:
            matched = _match_evidence(claim, evidence_items, self.max_evidence_per_claim)
            matched_by_claim[claim.id] = matched
            pair_rows.extend((f"{claim.id}::{item.id}", claim, item) for item in matched)

        failures: List[SearchFailure] = []
        model_name = getattr(self.classifier, "name", "unknown")
        try:
            decisions = self.classifier.classify_pairs(pair_rows)
        except Exception as exc:
            decisions = self.rule_fallback.classify_pairs(pair_rows)
            model_name = self.rule_fallback.name
            failures.append(SearchFailure(
                stage="claim_entailment",
                source=getattr(self.classifier, "name", "unknown"),
                code="ENTAILMENT_BACKEND_FAILED",
                message=public_error_message(exc),
            ))

        assessments = [
            self._assess_claim(claim, matched_by_claim[claim.id], decisions, profile)
            for claim in atomic_claims
        ]
        summary = self._summarize(assessments, failures, model_name)
        return VerifyResponse(
            query=query,
            profile=profile,
            assessments=assessments,
            trust_assessment=summary,
            search_boundary=search_boundary,
            failures=failures,
            elapsed_ms=int((self._monotonic() - started) * 1000),
        )

    def _assess_claim(
        self,
        claim: CandidateClaim,
        matched: Sequence[Evidence],
        decisions: Dict[str, EntailmentDecision],
        profile: str,
    ) -> ClaimAssessment:
        relations: List[ClaimEvidenceRelation] = []
        evidence_by_id = {item.id: item for item in matched}
        for item in matched:
            decision = decisions.get(
                f"{claim.id}::{item.id}",
                EntailmentDecision("unclear", "none", "没有蕴含判定结果"),
            )
            candidate_quote = decision.quote.strip()
            quote_is_literal = bool(candidate_quote and candidate_quote in item.passage.text)
            quote = candidate_quote if quote_is_literal else best_quote(claim, item)
            relation = decision.relation
            confidence = decision.confidence
            reason = decision.reason
            if relation in {"supports", "contradicts"} and not quote_is_literal:
                relation = "unclear"
                confidence = "none"
                reason = f"{reason}；判定引文无法逐字回到证据原文".strip("；")
            checks = _consistency_checks(claim, item, quote)
            relations.append(ClaimEvidenceRelation(
                evidence_id=item.id,
                relation=relation,
                confidence=confidence,
                reason=reason,
                quote=quote,
                locator=item.locator,
                evidence_quality=item.quality.level if item.quality else "unavailable",
                qualified=_relation_qualified(relation, item, checks),
                consistency_checks=checks,
            ))

        qualified_support = [r for r in relations if r.relation == "supports" and r.qualified]
        qualified_conflict = [r for r in relations if r.relation == "contradicts" and r.qualified]
        support_groups = {
            _independence_group(evidence_by_id[relation.evidence_id])
            for relation in qualified_support
        }
        primary_groups = {
            _independence_group(evidence_by_id[relation.evidence_id])
            for relation in qualified_support
            if _is_primary(evidence_by_id[relation.evidence_id], profile)
        }
        primary_count = len(primary_groups)
        policy_satisfied = bool(qualified_support) and (
            claim.importance != "key" or primary_count > 0 or len(support_groups) >= 2
        )

        gaps: List[str] = []
        if not matched:
            gaps.append("NO_MATCHED_EVIDENCE")
        all_support = [r for r in relations if r.relation == "supports"]
        if all_support and not qualified_support:
            gaps.append("NO_CITABLE_SUPPORT")
            for relation in all_support:
                evidence_item = evidence_by_id[relation.evidence_id]
                if evidence_item.quality:
                    gaps.extend(evidence_item.quality.reasons)
        if qualified_support and not policy_satisfied:
            gaps.append("NO_PRIMARY_OR_INDEPENDENT_SUPPORT")
        if qualified_conflict and not qualified_support:
            gaps.append("CLAIM_CONTRADICTED")
        if not all_support:
            gaps.append("NO_SUPPORTING_EVIDENCE")
        if claim.importance == "key":
            gaps.append("COUNTEREVIDENCE_NOT_SEARCHED")

        if qualified_support and qualified_conflict:
            status, confidence, review_required = "conflicted", "low", True
            gaps.append("SOURCE_CONFLICT")
        elif policy_satisfied:
            # Phase 1 尚未主动反证，置信度最高为 medium。
            status, confidence, review_required = "supported", "medium", False
        else:
            status, confidence = "insufficient", "low" if relations else "none"
            review_required = bool(qualified_conflict)

        followups = []
        if not qualified_support:
            followups.append(f"{claim.text} 原文")
        if claim.importance == "key":
            followups.append(f"{claim.text} 争议 反例")
        return ClaimAssessment(
            claim=claim,
            status=status,
            confidence=confidence,
            relations=relations,
            support_refs=_unique([r.evidence_id for r in relations if r.relation == "supports"]),
            conflict_refs=_unique([r.evidence_id for r in relations if r.relation == "contradicts"]),
            mention_refs=_unique([
                r.evidence_id for r in relations if r.relation in {"mentions", "unclear"}
            ]),
            independent_support_count=len(support_groups),
            primary_source_count=primary_count,
            counterevidence_searched=False,
            gaps=_unique(gaps),
            followup_queries=_unique(followups),
            review_required=review_required,
        )

    def _summarize(
        self,
        assessments: Sequence[ClaimAssessment],
        failures: Sequence[SearchFailure],
        model_name: str,
    ) -> TrustAssessment:
        total = len(assessments)
        supported = sum(item.status == "supported" for item in assessments)
        conflicted = sum(item.status == "conflicted" for item in assessments)
        insufficient = total - supported - conflicted
        covered = sum(any(relation.qualified for relation in item.relations) for item in assessments)
        if total and supported == total:
            status = "supported"
        elif supported or conflicted:
            status = "mixed"
        else:
            status = "insufficient"
        warnings = ["COUNTEREVIDENCE_NOT_SEARCHED"]
        if failures:
            warnings.append("ENTAILMENT_BACKEND_FALLBACK")
        return TrustAssessment(
            status=status,
            claims_total=total,
            supported_claims=supported,
            conflicted_claims=conflicted,
            insufficient_claims=insufficient,
            evidence_coverage_rate=covered / total if total else 0.0,
            unsupported_statement_rate=(total - supported) / total if total else 0.0,
            model=model_name,
            warnings=warnings,
        )


def build_claim_verifier(
    *,
    backend: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    max_claims: int,
    max_evidence_per_claim: int,
    http_session: Any = None,
    monotonic=time.monotonic,
) -> ClaimVerifier:
    backend = (backend or "auto").strip().lower()
    if backend not in {"auto", "rules", "siliconflow"}:
        raise ValueError(f"未知 TRUST_VERIFY_BACKEND: {backend}")
    use_model = backend == "siliconflow" or (backend == "auto" and bool(api_key))
    if use_model:
        classifier = SiliconFlowEntailmentClassifier(
            api_key, base_url, model, timeout, http_session=http_session
        )
    else:
        classifier = RuleEntailmentClassifier()
    return ClaimVerifier(
        classifier,
        max_claims=max_claims,
        max_evidence_per_claim=max_evidence_per_claim,
        monotonic=monotonic,
    )
