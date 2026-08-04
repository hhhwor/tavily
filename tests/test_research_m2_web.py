from __future__ import annotations

import socket
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.application.evidence_adoption import EvidenceAdoptionGate
from src.application.ports.runtime import Deadline
from src.application.research_execution import (
    BudgetLedger,
    CancellationToken,
    ExecutionContext,
)
from src.application.web_document_reader import WebDocumentReader
from src.bootstrap import build_container
from src.config import Settings
from src.domain.evidence import (
    Evidence,
    EvidenceAccess,
    EvidenceCitation,
    EvidenceDiagnostics,
    EvidenceLocator,
    EvidencePassage,
    EvidenceProvenance,
    EvidenceQuality,
)
from src.domain.research import ResearchPrivacy
from src.domain.search_api import (
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchQuery,
    SearchSeedSnapshot,
)
from src.infrastructure.safe_web_fetch import (
    PinnedIpHttpTransport,
    SafeHttpResponse,
    SafeWebFetchError,
    SafeWebFetcher,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> datetime:
        return datetime(2026, 8, 4, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value


class _Transport:
    def __init__(self, responses: dict[str, SafeHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        url,
        *,
        resolved_ip,
        timeout_seconds,
        headers,
    ):
        self.calls.append((url, resolved_ip))
        return self.responses[url]


class _ReaderFetcher:
    def __init__(self, *, noarchive: bool = False) -> None:
        self.noarchive = noarchive

    def fetch(self, url, *, deadline, allowed_mime=None):
        if url.endswith("/robots.txt"):
            return SafeHttpResponse(
                url=url,
                status=200,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=b"User-agent: *\nAllow: /\n",
                compressed_bytes=22,
            )
        html = """
        <html><head>
          <link rel="canonical" href="https://example.test/report">
          <link rel="license" href="https://creativecommons.org/licenses/by/4.0/">
        </head><body><article>
          <h1>Battery report</h1>
          <p id="result">实验结果表明，固态电池界面阻抗降低。</p>
        </article></body></html>
        """.encode()
        headers = {
            "content-type": "text/html; charset=utf-8",
            "etag": '"web-v1"',
            "last-modified": "Tue, 04 Aug 2026 00:00:00 GMT",
        }
        if self.noarchive:
            headers["x-robots-tag"] = "noarchive"
        return SafeHttpResponse(
            url=url,
            status=200,
            headers=headers,
            body=html,
            compressed_bytes=len(html),
        )


class _RawResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    def read(self, amount, decode_content=False):
        return next(self._chunks, b"")


def _resolver_for(mapping: dict[str, str]):
    def resolve(host, port, *, type):
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]

    return resolve


def _context() -> ExecutionContext:
    clock = _Clock()
    return ExecutionContext(
        research_id="rsch_web",
        attempt=1,
        policy_id="technical-evidence.v1",
        privacy=ResearchPrivacy(),
        deadline=Deadline.after(30_000, clock),
        cancellation=CancellationToken(),
        budget=BudgetLedger({}, monotonic=clock.monotonic),
    )


def _candidate() -> Evidence:
    return Evidence(
        id="web:report:content",
        result_id="web:report",
        type="web",
        source="provider",
        title="Battery report",
        url="https://example.test/report?tracking=1",
        passage=EvidencePassage(
            text="供应商摘录称固态电池界面阻抗降低。",
            snippet_type="web_content",
        ),
        citation=EvidenceCitation(label="example.test", venue="example.test"),
        access=EvidenceAccess(is_open=True),
        diagnostics=EvidenceDiagnostics(
            warnings=["PROVIDER_EXTRACT_NOT_ORIGINAL"]
        ),
        provenance=EvidenceProvenance(
            canonical_url="https://example.test/report?tracking=1",
            publisher_id="domain:example.test",
            ownership_group="domain:example.test",
            content_origin="provider_extract",
            document_id="https://example.test/report?tracking=1",
            retrieved_at="2026-08-04T00:00:00Z",
        ),
        quality=EvidenceQuality(
            level="limited",
            reasons=["PROVIDER_EXTRACT_NOT_ORIGINAL", "NO_STABLE_LOCATOR"],
        ),
    )


def test_safe_fetcher_blocks_private_initial_and_redirect_addresses():
    deadline = _context().deadline
    transport = _Transport({})
    private = SafeWebFetcher(
        transport=transport,
        resolver=_resolver_for({"private.test": "127.0.0.1"}),
    )
    with pytest.raises(SafeWebFetchError) as caught:
        private.fetch("http://private.test/", deadline=deadline)
    assert caught.value.code == "WEB_SSRF_ADDRESS_BLOCKED"
    assert transport.calls == []

    redirect = SafeHttpResponse(
        url="https://public.test/start",
        status=302,
        headers={
            "location": "http://internal.test/secret",
            "content-type": "text/html",
        },
        body=b"",
        compressed_bytes=0,
    )
    transport = _Transport({"https://public.test/start": redirect})
    fetcher = SafeWebFetcher(
        transport=transport,
        resolver=_resolver_for({
            "public.test": "93.184.216.34",
            "internal.test": "169.254.169.254",
        }),
    )
    with pytest.raises(SafeWebFetchError) as caught:
        fetcher.fetch("https://public.test/start", deadline=deadline)
    assert caught.value.code == "WEB_SSRF_ADDRESS_BLOCKED"
    assert transport.calls == [
        ("https://public.test/start", "93.184.216.34")
    ]


def test_transport_enforces_compression_ratio():
    import gzip

    compressed = gzip.compress(b"A" * 20_000)
    transport = PinnedIpHttpTransport(
        max_compressed_bytes=100_000,
        max_decoded_bytes=100_000,
        max_compression_ratio=2,
    )
    with pytest.raises(SafeWebFetchError) as caught:
        transport._read_body(  # noqa: SLF001
            _RawResponse([compressed]),
            {"content-encoding": "gzip"},
        )
    assert caught.value.code == "WEB_COMPRESSION_RATIO_EXCEEDED"


def test_transport_enforces_total_body_deadline():
    ticks = iter((0.0, 2.0))
    transport = PinnedIpHttpTransport(monotonic=lambda: next(ticks))
    with pytest.raises(SafeWebFetchError) as caught:
        transport._read_body(  # noqa: SLF001
            _RawResponse([b"first chunk"]),
            {},
            expires_at=1.0,
        )
    assert caught.value.code == "WEB_FETCH_DEADLINE_EXCEEDED"


def test_web_reader_builds_canonical_paragraph_locator_and_version():
    candidate = _candidate()
    result = WebDocumentReader(
        _ReaderFetcher(),
        now=_Clock().now,
    ).read(candidate, context=_context())

    assert result.status == "ready"
    assert result.version is not None
    assert result.version.canonical_uri == "https://example.test/report"
    assert result.version.etag == '"web-v1"'
    assert result.version.content_hash.startswith("sha256:")
    assert result.version.storage_mode == "full_text"
    assert result.chunks[0].locator.paragraph_id == "result"
    assert result.chunks[0].locator.section == "Battery report"

    adopted = EvidenceAdoptionGate().adopt(
        candidate,
        result,
        claim_texts=["固态电池界面阻抗降低"],
    )
    assert adopted[0].passage.snippet_type == "web_original"
    assert adopted[0].quality is not None
    assert adopted[0].quality.can_support_key_claim is True
    assert adopted[0].provenance is not None
    assert adopted[0].provenance.syndication_group == (
        result.version.independent_work_id
    )


def test_noarchive_page_cannot_become_qualified_support():
    candidate = _candidate()
    result = WebDocumentReader(
        _ReaderFetcher(noarchive=True),
    ).read(candidate, context=_context())

    assert result.version is not None
    assert result.version.storage_mode == "locator_only"
    adopted = EvidenceAdoptionGate().adopt(candidate, result)
    assert adopted[0].quality is not None
    assert adopted[0].quality.can_support_key_claim is False
    assert "ORIGINAL_STORAGE_NOT_PERMITTED" in adopted[0].quality.reasons


def test_research_runner_persists_and_resolves_web_original(tmp_path):
    settings = Settings(
        openalex_enabled=False,
        patent_es_enabled=False,
        ranking_profile="fast",
        rerank_threshold_mode="off",
        mcp_mode="false",
        state_db_path=str(tmp_path / "state.sqlite3"),
        research_max_workers=1,
    )
    container = build_container(settings, include_mcp=False)
    container.engine._research_service._runner._document_readers[  # noqa: SLF001
        "web"
    ] = WebDocumentReader(_ReaderFetcher(), now=_Clock().now)
    snapshot = SearchSeedSnapshot(
        requested_source_types=["web"],
        planned_source_types=["web"],
        query=SearchQuery(
            original="固态电池界面阻抗降低",
            effective="固态电池界面阻抗降低",
            filters_requested=RequestedFilters(),
        ),
        evidence=[_candidate()],
        retrieval_assessment=RetrievalAssessment(status="limited"),
        retrieval_boundary=RetrievalBoundary(
            query_time=datetime(2026, 8, 4, tzinfo=timezone.utc),
            deadline_ms=30_000,
        ),
    )
    seed = container.seed_store.save(snapshot, ttl_seconds=3600)
    with TestClient(create_app(container)) as client:
        started = client.post(
            "/research",
            headers={"Idempotency-Key": "m2-web-runner"},
            json={
                "search_id": seed.search_id,
                "profile": "technology_landscape",
                "depth": "quick",
                "objective": {
                    "question": "固态电池界面阻抗降低",
                    "claims": [{"text": "固态电池界面阻抗降低"}],
                },
            },
        )
        assert started.status_code == 202
        research_id = started.json()["research_id"]
        task = {}
        for _ in range(200):
            task = client.get(
                f"/research/{research_id}?detail=full"
            ).json()
            if task["state"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.01)

        # One web document is locatable, but the profile still requires
        # primary or independent corroboration.
        assert task["state"] == "partial"
        original = next(
            item for item in task["dossier"]["evidence_index"].values()
            if item["passage"]["snippet_type"] == "web_original"
        )
        locator = EvidenceLocator.model_validate(original["locator"])
        assert container.research_store.resolve_locator(
            research_id,
            locator,
        ) == original["passage"]["text"]
