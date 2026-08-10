"""Deterministic Research exports and retained artifact access."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import timedelta

from src.application.ports.research_store import (
    ResearchStore,
    StoredResearchArtifact,
)
from src.application.ports.runtime import Clock
from src.domain.research import ResearchArtifact, ResearchDossier


class ResearchArtifactNotFound(LookupError):
    pass


class ResearchArtifactExpired(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class RenderedArtifact:
    kind: str
    media_type: str
    filename: str
    content: bytes


class ResearchArtifactRenderer:
    """Render human and machine views only from the audited Dossier."""

    version = "research-artifacts.v1"

    def render(self, dossier: ResearchDossier) -> list[RenderedArtifact]:
        if (
            dossier.citation_audit is None
            or dossier.citation_audit.status != "passed"
        ):
            raise ValueError("citation audit 未通过，禁止生成 Research artifact")
        return [
            RenderedArtifact(
                kind="dossier_json",
                media_type="application/json",
                filename="research-dossier.json",
                content=self._dossier_json(dossier),
            ),
            RenderedArtifact(
                kind="report_markdown",
                media_type="text/markdown; charset=utf-8",
                filename="research-report.md",
                content=self._markdown(dossier),
            ),
            RenderedArtifact(
                kind="evidence_csv",
                media_type="text/csv; charset=utf-8",
                filename="research-evidence.csv",
                content=self._csv(dossier),
            ),
            RenderedArtifact(
                kind="evidence_jsonl",
                media_type="application/x-ndjson",
                filename="research-evidence.jsonl",
                content=self._jsonl(dossier),
            ),
        ]

    @staticmethod
    def _dossier_json(dossier: ResearchDossier) -> bytes:
        payload = dossier.model_dump(
            mode="json",
            exclude={"artifact_index", "artifacts"},
        )
        return (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _markdown(dossier: ResearchDossier) -> bytes:
        summary = dossier.summary
        lines = ["# Research Dossier", ""]
        if summary is not None:
            summary_refs = " ".join(
                f"[finding:{item}]" for item in summary.key_finding_refs
            )
            lines.extend([
                "## Summary",
                "",
                f"{summary.headline} {summary_refs}".rstrip(),
                "",
            ])
        lines.extend(["## Statements", ""])
        finding_by_id = {item.id: item for item in dossier.findings}
        for statement in dossier.statements:
            refs: list[str] = []
            for finding_ref in statement.finding_refs:
                refs.append(f"finding:{finding_ref}")
                finding = finding_by_id.get(finding_ref)
                if finding is None:
                    continue
                evidence_refs = (
                    finding.qualified_relation_refs
                    + finding.conflict_relation_refs
                )
                refs.extend(f"evidence:{item}" for item in evidence_refs)
            suffix = " ".join(f"[{item}]" for item in dict.fromkeys(refs))
            lines.append(f"- {statement.text} {suffix}".rstrip())
        if dossier.conflicts:
            lines.extend(["", "## Conflicts", ""])
            for conflict in dossier.conflicts:
                refs = " ".join(
                    f"[evidence:{item}]"
                    for item in (
                        conflict.support_evidence_refs
                        + conflict.conflict_evidence_refs
                    )
                )
                lines.append(f"- {conflict.message} {refs}".rstrip())
        if dossier.limitations_detail:
            lines.extend(["", "## Limitations", ""])
            for limitation in dossier.limitations_detail:
                lines.append(f"- `{limitation.code}`: {limitation.message}")
        methods = dossier.methods
        lines.extend(["", "## Methods", ""])
        if methods is not None:
            lines.extend([
                f"- Profile: `{methods.profile}`",
                f"- Policy: `{methods.policy_id}` / `{methods.policy_version}`",
                f"- Execution route: `{methods.execution_route}`",
                f"- Synthesis: `{methods.synthesis_mode}` / `{methods.synthesis_version}`",
                f"- Evidence revision: `{methods.evidence_set_revision}`",
                f"- Stop reason: `{methods.stop_reason}`",
            ])
        lines.extend(["", "## Evidence locators", ""])
        for finding in dossier.findings:
            for relation in finding.assessment.relations:
                if not relation.qualified:
                    continue
                locator = relation.locator
                if locator is None:
                    continue
                location = ":".join(str(item) for item in (
                    locator.document_id,
                    locator.version_id or "",
                    locator.chunk_index if locator.chunk_index is not None else "",
                ))
                lines.append(
                    f"- `[evidence:{relation.evidence_id}]` "
                    f"{relation.relation}; locator `{location}`; quote: "
                    f"> {relation.quote.replace(chr(10), ' ')}"
                )
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

    @classmethod
    def _rows(cls, dossier: ResearchDossier) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for finding in dossier.findings:
            for relation in finding.assessment.relations:
                locator = relation.locator
                evidence = dossier.evidence_index.get(relation.evidence_id)
                rows.append({
                    "finding_id": finding.id,
                    "claim_id": finding.claim.id,
                    "claim": finding.claim.text,
                    "finding_status": finding.assessment.status,
                    "evidence_id": relation.evidence_id,
                    "evidence_type": evidence.type if evidence else "",
                    "title": evidence.title if evidence else "",
                    "relation": relation.relation,
                    "qualified": relation.qualified,
                    "quote": relation.quote,
                    "document_id": locator.document_id if locator else "",
                    "version_id": locator.version_id if locator else "",
                    "chunk_index": (
                        locator.chunk_index if locator is not None else None
                    ),
                    "char_start": locator.char_start if locator else None,
                    "char_end": locator.char_end if locator else None,
                    "quality": relation.evidence_quality,
                })
        return rows

    @classmethod
    def _csv(cls, dossier: ResearchDossier) -> bytes:
        rows = cls._rows(dossier)
        columns = [
            "finding_id", "claim_id", "claim", "finding_status",
            "evidence_id", "evidence_type", "title", "relation",
            "qualified", "quote", "document_id", "version_id",
            "chunk_index", "char_start", "char_end", "quality",
        ]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows([
            {
                key: cls._csv_safe(value)
                for key, value in row.items()
            }
            for row in rows
        ])
        return buffer.getvalue().encode("utf-8")

    @staticmethod
    def _csv_safe(value: object) -> object:
        """Prevent untrusted evidence text from becoming a spreadsheet formula."""
        if not isinstance(value, str):
            return value
        if value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    @classmethod
    def _jsonl(cls, dossier: ResearchDossier) -> bytes:
        return (
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in cls._rows(dossier)
            )
        ).encode("utf-8")


class ResearchArtifactService:
    def __init__(
        self,
        *,
        store: ResearchStore,
        clock: Clock,
        retention_seconds: int,
        renderer: ResearchArtifactRenderer | None = None,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("artifact retention_seconds 必须大于 0")
        self._store = store
        self._clock = clock
        self._retention_seconds = retention_seconds
        self._renderer = renderer or ResearchArtifactRenderer()

    def create_all(
        self,
        research_id: str,
        dossier: ResearchDossier,
        *,
        evidence_set_revision: int,
    ) -> list[ResearchArtifact]:
        artifacts: list[ResearchArtifact] = []
        for rendered in self._renderer.render(dossier):
            digest = hashlib.sha256(rendered.content).hexdigest()
            artifact_id = "artifact_" + hashlib.sha256(
                "|".join((
                    research_id,
                    rendered.kind,
                    str(evidence_set_revision),
                    self._renderer.version,
                    digest,
                )).encode("utf-8")
            ).hexdigest()[:20]
            existing = self._store.get_artifact(
                research_id,
                artifact_id=artifact_id,
            )
            if (
                existing is not None
                and existing.metadata.expires_at > self._clock.now()
            ):
                artifacts.append(existing.metadata)
                continue
            created_at = self._clock.now()
            metadata = ResearchArtifact(
                artifact_id=artifact_id,
                kind=rendered.kind,
                media_type=rendered.media_type,
                filename=rendered.filename,
                href=f"/research/{research_id}/artifacts/{artifact_id}",
                size_bytes=len(rendered.content),
                sha256=digest,
                evidence_set_revision=evidence_set_revision,
                renderer_version=self._renderer.version,
                created_at=created_at,
                expires_at=created_at + timedelta(
                    seconds=self._retention_seconds
                ),
                citation_audit_status="passed",
            )
            self._store.save_artifact(
                research_id,
                metadata=metadata,
                content=rendered.content,
            )
            artifacts.append(metadata)
        return artifacts

    def get(
        self,
        research_id: str,
        artifact_id: str,
    ) -> StoredResearchArtifact:
        stored = self._store.get_artifact(
            research_id,
            artifact_id=artifact_id,
        )
        if stored is None:
            raise ResearchArtifactNotFound(artifact_id)
        if stored.metadata.expires_at <= self._clock.now():
            raise ResearchArtifactExpired(artifact_id)
        return stored
