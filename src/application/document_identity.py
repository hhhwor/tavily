"""Document-version and independent-work identity normalization."""
from __future__ import annotations

import hashlib
import re

from src.domain.evidence import Evidence


_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)


def normalize_doi(value: str | None) -> str:
    return _DOI_PREFIX.sub("", (value or "").strip()).casefold()


def academic_independent_work_id(item: Evidence) -> str:
    doi = normalize_doi(item.citation.doi)
    if doi:
        return f"academic-doi:{doi}"
    work_id = (item.citation.work_id or "").strip().casefold()
    if work_id:
        return f"academic-openalex:{work_id}"
    return f"academic-result:{item.result_id}"


def academic_document_version_id(
    item: Evidence,
    content_hash: str,
) -> str:
    identity = "|".join((
        academic_independent_work_id(item),
        (item.citation.work_id or "").strip().casefold(),
        content_hash,
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"academic-version:{digest}"


def web_document_version_id(canonical_url: str, content_hash: str) -> str:
    digest = hashlib.sha256(
        f"{canonical_url.casefold()}|{content_hash}".encode("utf-8")
    ).hexdigest()
    return f"web-version:{digest}"


def patent_document_version_id(
    publication_number: str,
    content_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{publication_number.casefold()}|{content_hash}".encode("utf-8")
    ).hexdigest()
    return f"patent-version:{digest}"


def independent_work_id(item: Evidence) -> str:
    if item.type == "academic":
        return academic_independent_work_id(item)
    if item.type == "patent" and item.patent is not None:
        value = (
            item.patent.family_id
            or item.patent.publication_number
            or item.result_id
        )
        return f"patent-family:{value}"
    if item.provenance is not None:
        value = (
            item.provenance.syndication_group
            or item.provenance.canonical_url
            or item.provenance.document_id
            or item.result_id
        )
        return f"web-work:{value}"
    return item.result_id


def evidence_version_key(item: Evidence) -> str:
    version_id = (
        item.locator.version_id
        if item.locator is not None
        else None
    ) or (
        item.provenance.version_id
        if item.provenance is not None
        else None
    )
    locator = item.locator
    unit = "|".join((
        str(locator.chunk_index if locator else ""),
        str(locator.char_start if locator else ""),
        str(locator.char_end if locator else ""),
        str(locator.page_from if locator else ""),
        str(locator.page_to if locator else ""),
    ))
    return f"{version_id or item.id}|{unit}"
