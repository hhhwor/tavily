"""Read-only Elasticsearch adapter for patent full text and relationships."""
from __future__ import annotations

import re
from typing import Any, Iterable

import requests

from src.application.ports.patent_text import PatentTextGateway
from src.application.ports.runtime import Deadline
from src.domain.patent_text import PatentDocumentRecord, PatentTextUnit


_FIELDS = [
    "publication_number",
    "application_number",
    "family_id",
    "priority_root",
    "priority_numbers",
    "priority_dates",
    "application_date",
    "publication_date",
    "claims",
    "claim_text",
    "description",
    "description_paragraphs",
    "family_members",
    "citations",
    "patent_citations",
    "npl_citations",
    "license",
]
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        for key in ("publication_number", "number", "id", "text", "value"):
            if value.get(key):
                return _strings(value[key])
        return []
    if isinstance(value, Iterable):
        result: list[str] = []
        for item in value:
            result.extend(_strings(item))
        return list(dict.fromkeys(result))
    return [str(value)]


def _units(value: Any, kind: str) -> list[PatentTextUnit]:
    if value is None:
        return []
    rows = value if isinstance(value, list) else [value]
    units: list[PatentTextUnit] = []
    for index, row in enumerate(rows, start=1):
        if isinstance(row, dict):
            text = str(
                row.get("text")
                or row.get("claim_text")
                or row.get("content")
                or ""
            ).strip()
            identifier = str(
                row.get("claim_number")
                or row.get("paragraph_id")
                or row.get("number")
                or row.get("id")
                or index
            ).strip()
            section = str(row.get("section") or "").strip() or None
            if text:
                units.append(PatentTextUnit(
                    kind=kind,
                    identifier=identifier,
                    text=text,
                    section=section,
                ))
            continue
        text = str(row or "").strip()
        if not text:
            continue
        paragraphs = (
            _PARAGRAPH_BREAK.split(text)
            if kind == "description" else [text]
        )
        for offset, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if paragraph:
                units.append(PatentTextUnit(
                    kind=kind,
                    identifier=str(index + offset),
                    text=paragraph,
                ))
    return units


class PatentEsFullTextGateway(PatentTextGateway):
    def __init__(
        self,
        *,
        base_url: str,
        index: str,
        http_session: Any = requests,
        timeout_seconds: float = 15.0,
        verify_tls: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._index = index
        self._http = http_session
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._verify_tls = verify_tls

    def fetch(
        self,
        publication_number: str,
        *,
        deadline: Deadline,
    ) -> PatentDocumentRecord:
        publication_number = publication_number.strip()
        if not publication_number:
            return self._failure("", "PATENT_PUBLICATION_NUMBER_MISSING", False)
        remaining = deadline.remaining_seconds()
        if remaining <= 0:
            return self._failure(
                publication_number,
                "PATENT_FULLTEXT_DEADLINE_EXCEEDED",
                True,
            )
        body = {
            "size": 3,
            "_source": _FIELDS,
            "query": {
                "bool": {
                    "should": [
                        {"term": {
                            "publication_number.keyword": publication_number
                        }},
                        {"term": {"publication_number": publication_number}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        }
        try:
            response = self._http.post(
                f"{self._base_url}/{self._index}/_search",
                json=body,
                timeout=min(self._timeout_seconds, remaining),
                verify=self._verify_tls,
            )
            response.raise_for_status()
            hits = response.json().get("hits", {}).get("hits", [])
        except requests.Timeout:
            return self._failure(
                publication_number, "PATENT_FULLTEXT_TIMEOUT", True
            )
        except Exception:
            return self._failure(
                publication_number, "PATENT_FULLTEXT_READ_FAILED", True
            )
        hit = next((
            item for item in hits
            if str((item.get("_source") or {}).get(
                "publication_number", ""
            )).casefold() == publication_number.casefold()
        ), hits[0] if hits else None)
        if hit is None:
            return self._failure(
                publication_number, "PATENT_DOCUMENT_NOT_FOUND", False
            )
        source = hit.get("_source") or {}
        claims = _units(source.get("claims") or source.get("claim_text"), "claim")
        descriptions = _units(
            source.get("description_paragraphs") or source.get("description"),
            "description",
        )
        if not claims and not descriptions:
            return self._failure(
                publication_number, "PATENT_FULLTEXT_UNAVAILABLE", False
            )
        priority_roots = _strings(
            source.get("priority_root") or source.get("priority_numbers")
        )
        source_version_id = ":".join(str(value) for value in (
            hit.get("_index", ""),
            hit.get("_id", ""),
            hit.get("_version", ""),
        ) if value != "") or None
        return PatentDocumentRecord(
            status="ready",
            publication_number=str(
                source.get("publication_number") or publication_number
            ),
            application_number=str(source.get("application_number") or ""),
            family_id=str(source.get("family_id") or ""),
            priority_root=priority_roots[0] if priority_roots else "",
            priority_dates=_strings(source.get("priority_dates")),
            application_date=str(source.get("application_date") or ""),
            publication_date=str(source.get("publication_date") or ""),
            units=[*claims, *descriptions],
            family_members=_strings(source.get("family_members")),
            patent_citations=_strings(
                source.get("patent_citations") or source.get("citations")
            ),
            npl_citations=_strings(source.get("npl_citations")),
            source_version_id=source_version_id,
            license=(str(source.get("license")) if source.get("license") else None),
        )

    @staticmethod
    def _failure(
        publication_number: str,
        code: str,
        retryable: bool,
    ) -> PatentDocumentRecord:
        return PatentDocumentRecord(
            status="failed" if retryable else "unavailable",
            publication_number=publication_number,
            failure_code=code,
            retryable=retryable,
        )
