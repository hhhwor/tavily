"""Phase 0 可信旁路标注。

只补充 provenance、locator、content quality 和 SearchBoundary，不改变相关性分数、
Evidence 顺序或现有 answerability。陈述级支持/冲突校验属于后续 Phase。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from src.domain.evidence import (
    Evidence,
    EvidenceFieldProvenance,
    EvidenceLocator,
    EvidenceProvenance,
    EvidenceQuality,
    SearchBoundary,
)
from src.pipeline.dedup import normalize_url

_NON_ID = re.compile(r"[^a-z0-9]+")


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_url(url: str) -> str:
    """保留 http(s) scheme，同时复用现有 URL 身份归一化规则。"""
    if not url:
        return ""
    original = urlsplit(url if "://" in url else f"https://{url}")
    normalized = normalize_url(url)
    if normalized.startswith("//"):
        scheme = original.scheme if original.scheme in {"http", "https"} else "https"
        return f"{scheme}:{normalized}"
    return normalized


def _publisher(evidence: Evidence, canonical_url: str) -> tuple[str, str, str]:
    if evidence.type == "patent":
        country = (evidence.patent.country if evidence.patent else "").strip().lower()
        suffix = country or "unknown"
        name = f"{country.upper()} patent authority" if country else "Patent authority unknown"
        return f"patent-authority:{suffix}", name, "patent_authority"
    if evidence.type == "academic":
        venue = (evidence.citation.venue or evidence.citation.label or "").strip()
        venue_id = _NON_ID.sub("-", venue.lower()).strip("-") or "unknown"
        return f"academic-venue:{venue_id}", venue or "Academic venue unknown", "academic_venue"
    host = urlsplit(canonical_url).netloc.lower()
    name = evidence.citation.venue or evidence.citation.label or host
    return (f"domain:{host}" if host else "domain:unknown"), name, "website"


def _document_identity(evidence: Evidence, canonical_url: str) -> tuple[str, Optional[str], Optional[str]]:
    if evidence.type == "academic":
        record_id = evidence.citation.work_id or evidence.citation.doi
        document_id = record_id or evidence.result_id
        version_id = evidence.citation.doi or None
        return document_id, version_id, record_id or None
    if evidence.type == "patent":
        publication = evidence.citation.publication_number
        if not publication and evidence.patent:
            publication = evidence.patent.publication_number
        document_id = publication or evidence.result_id
        return document_id, publication or None, publication or None
    return canonical_url or evidence.result_id, None, canonical_url or None


def _content_origin(evidence: Evidence) -> str:
    return {
        "pdf_text": "fulltext",
        "abstract": "metadata",
        "patent_abstract": "metadata",
        "web_content": "provider_extract",
        "web_snippet": "snippet",
    }.get(evidence.passage.snippet_type, "unknown")


def _field_provenance(evidence: Evidence, origin: str) -> dict[str, EvidenceFieldProvenance]:
    passage_field = {
        "fulltext": "pdf_text",
        "metadata": "abstract",
        "provider_extract": "content",
        "snippet": "snippet",
    }.get(origin, "unknown")
    transformations = []
    if origin == "fulltext":
        transformations.append("pdf_extract")
    elif origin in {"metadata", "provider_extract", "snippet"}:
        transformations.append("provider_mapping")
    if evidence.diagnostics.partial:
        transformations.append("truncate")
    return {
        "title": EvidenceFieldProvenance(
            source_field="title", retrieved_via=evidence.source,
            transformations=["provider_mapping"],
        ),
        "published_date": EvidenceFieldProvenance(
            source_field="publication_date|date", retrieved_via=evidence.source,
            transformations=["provider_mapping"],
        ),
        "passage.text": EvidenceFieldProvenance(
            source_field=passage_field, retrieved_via=evidence.source,
            transformations=transformations,
        ),
    }


def _locator(
    evidence: Evidence,
    document_id: str,
    version_id: Optional[str],
) -> EvidenceLocator:
    section = None
    if evidence.passage.snippet_type == "abstract":
        section = "abstract"
    elif evidence.passage.snippet_type == "patent_abstract":
        section = "abstract"
    return EvidenceLocator(
        document_id=document_id,
        version_id=version_id,
        section=section,
        page_from=evidence.passage.page_from,
        page_to=evidence.passage.page_to,
        char_start=evidence.passage.char_start,
        char_end=evidence.passage.char_end,
        chunk_index=evidence.passage.chunk_index,
    )


def _quality(evidence: Evidence, locator: EvidenceLocator, origin: str) -> EvidenceQuality:
    if not evidence.passage.text.strip():
        return EvidenceQuality(level="unavailable", reasons=["EMPTY_EVIDENCE"])

    stable_document = bool(locator.document_id and locator.version_id)
    stable_unit = bool(
        locator.paragraph_id
        or locator.page_from is not None
        or locator.table_id
        or locator.figure_id
        or locator.claim_number
        or locator.section == "abstract"
    )
    has_stable_locator = stable_document and stable_unit

    if origin == "fulltext":
        if has_stable_locator:
            return EvidenceQuality(
                level="citable", is_original=True, has_stable_locator=True,
                can_support_key_claim=True,
            )
        return EvidenceQuality(
            level="limited", is_original=True, has_stable_locator=False,
            reasons=["NO_STABLE_LOCATOR"],
        )
    if evidence.type == "academic" and origin == "metadata":
        return EvidenceQuality(
            level="discovery_only", has_stable_locator=has_stable_locator,
            reasons=["ABSTRACT_ONLY"],
        )
    if evidence.type == "patent":
        return EvidenceQuality(
            level="discovery_only", has_stable_locator=has_stable_locator,
            reasons=["PATENT_ABSTRACT_ONLY", "CLAIM_TEXT_UNAVAILABLE"],
        )
    if origin == "snippet":
        return EvidenceQuality(
            level="discovery_only", reasons=["SNIPPET_ONLY", "NO_STABLE_LOCATOR"],
        )
    if origin == "provider_extract":
        reasons = ["PROVIDER_EXTRACT_NOT_ORIGINAL", "NO_STABLE_LOCATOR"]
        if "+" in evidence.source:
            reasons.append("MULTI_PROVIDER_ORIGIN_AMBIGUOUS")
        return EvidenceQuality(level="limited", reasons=reasons)
    return EvidenceQuality(level="limited", reasons=["CONTENT_ORIGIN_UNKNOWN"])


def _append_quality_warnings(evidence: Evidence, quality: EvidenceQuality) -> None:
    for reason in quality.reasons:
        if reason not in evidence.diagnostics.warnings:
            evidence.diagnostics.warnings.append(reason)


def annotate_evidence(
    evidence: Sequence[Evidence],
    *,
    retrieved_at: Optional[datetime] = None,
) -> List[Evidence]:
    """在深拷贝上补充 Phase 0 字段，不修改调用方 Evidence。"""
    timestamp = _utc_iso(retrieved_at or datetime.now(timezone.utc))
    annotated = [item.model_copy(deep=True) for item in evidence]
    for item in annotated:
        canonical_url = _canonical_url(item.url)
        publisher_id, publisher_name, publisher_type = _publisher(item, canonical_url)
        document_id, version_id, source_record_id = _document_identity(item, canonical_url)
        origin = _content_origin(item)
        item.provenance = EvidenceProvenance(
            canonical_url=canonical_url,
            publisher_id=publisher_id,
            publisher_name=publisher_name,
            publisher_type=publisher_type,
            retrieved_via=item.source,
            content_origin=origin,
            document_id=document_id,
            version_id=version_id,
            source_record_id=source_record_id,
            published_at=item.published_date or None,
            updated_at=item.updated_date,
            retrieved_at=timestamp,
            ownership_group=publisher_id,
            license=item.access.license,
            original_language=item.language,
            parser_version=None,
            field_provenance=_field_provenance(item, origin),
        )
        item.locator = _locator(item, document_id, version_id)
        item.quality = _quality(item, item.locator, origin)
        _append_quality_warnings(item, item.quality)
    return annotated


def _detect_languages(query: str) -> List[str]:
    has_cjk = any("\u3400" <= char <= "\u9fff" for char in query)
    has_latin = any(("a" <= char.lower() <= "z") for char in query)
    languages = []
    if has_cjk:
        languages.append("zh")
    if has_latin:
        languages.append("en")
    return languages or ["und"]


def build_search_boundary(
    *,
    query: str,
    source_names: Iterable[str],
    evidence: Sequence[Evidence],
    query_time: Optional[datetime] = None,
    source_snapshot: Optional[Mapping[str, str]] = None,
    max_candidates: int = 0,
    deadline_ms: Optional[int] = None,
) -> SearchBoundary:
    """按本次真实配置构建单轮检索边界；未知能力显式写入 limitations。"""
    names = sorted({part for name in source_names for part in name.split("+") if part})
    snapshots = {
        name: (source_snapshot or {}).get(name, "snapshot-unavailable")
        for name in names
    }
    limitations = ["SINGLE_ROUND_SEARCH", "JURISDICTION_NOT_FILTERED"]
    if deadline_ms is None:
        limitations.append("NO_GLOBAL_DEADLINE")
    for name, snapshot in snapshots.items():
        if (
            snapshot in {"snapshot-unavailable", "provider-managed"}
            or "alias:" in snapshot
            or "unspecified" in snapshot
        ):
            limitations.append(f"SOURCE_SNAPSHOT_NOT_IMMUTABLE:{name}")

    licenses = sorted({
        item.provenance.license or "unspecified"
        for item in evidence
        if item.provenance is not None
    }) or ["unspecified"]
    return SearchBoundary(
        source_snapshot=snapshots,
        query_time=_utc_iso(query_time or datetime.now(timezone.utc)),
        languages=_detect_languages(query),
        jurisdictions=[],
        license_scope=licenses,
        max_rounds=1,
        max_candidates=max(0, max_candidates),
        deadline_ms=deadline_ms,
        limitations=limitations,
    )
