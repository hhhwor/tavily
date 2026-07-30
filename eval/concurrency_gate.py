"""Deterministic 20/50-concurrency stability gate for the search pipeline.

The gate exercises the real planning, recall, retry, ranking, evidence,
trust-annotation and seed-store path.  Only the external provider is replaced
with a controlled, latency-bearing fixture so the result is reproducible and
does not consume third-party quota.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Lock
from typing import Any, Sequence
from urllib.parse import quote

from src.application.answerability import AnswerabilityPolicy
from src.application.commands import SearchCommand
from src.application.discovery_service import DiscoveryService
from src.application.evidence_assembler import EvidenceAssembler
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.search_service import SearchService
from src.application.source_registry import SourceRegistry
from src.application.trust_annotator import TrustAnnotator
from src.config import Settings
from src.domain.errors import ExternalServiceError
from src.domain.search import SearchResult
from src.infrastructure.resilience import ResilienceManager
from src.infrastructure.runtime import SystemClock
from src.infrastructure.sqlite_seed_store import SqliteSearchSeedStore
from src.providers.base import SearchProvider
from src.ranking.ports import Reranker
from src.application.ports.retrieval import SourceDescriptor


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_THRESHOLDS = ROOT / "eval" / "golden" / "concurrency_thresholds.json"
DEFAULT_REPORT = ROOT / "eval" / "golden" / "concurrency_report.json"
RUNNER_VERSION = "concurrency-gate.v1"
DEFAULT_LEVELS = (20, 50)
_REQUEST_INDEX = re.compile(r"(\d+)$")


class ControlledSearchProvider(SearchProvider):
    """Thread-safe provider fixture with real latency and recoverable faults."""

    name = "controlled-web"
    descriptor = SourceDescriptor(
        id=name,
        kind="web",
        capabilities=frozenset({"full_content", "language_filter"}),
        snapshot_capability="service_index",
        default_snapshot="controlled-corpus.v1",
        data_license="test-fixture",
        max_candidates=5,
        count_empty_as_used=True,
    )

    def __init__(
        self,
        *,
        latency_ms: float,
        transient_every: int,
    ) -> None:
        self._latency_seconds = max(0.0, latency_ms / 1000)
        self._transient_every = max(0, transient_every)
        self._lock = Lock()
        self._attempts: dict[str, int] = {}
        self._in_flight = 0
        self._max_in_flight = 0
        self._calls = 0
        self._injected_queries: set[str] = set()
        self._recovered_queries: set[str] = set()

    @staticmethod
    def _request_index(query: str) -> int:
        match = _REQUEST_INDEX.search(query)
        return int(match.group(1)) if match else -1

    def search(
        self,
        query: str,
        top_k: int = 10,
        recency: str | None = None,
    ) -> list[SearchResult]:
        with self._lock:
            self._calls += 1
            attempt = self._attempts.get(query, 0) + 1
            self._attempts[query] = attempt
            should_inject = (
                self._transient_every > 0
                and self._request_index(query) % self._transient_every == 0
                and attempt == 1
            )
            if should_inject:
                self._injected_queries.add(query)
        if should_inject:
            raise ExternalServiceError(
                provider=self.name,
                code="CONTROLLED_TRANSIENT",
                recoverable=True,
            )

        with self._lock:
            self._in_flight += 1
            self._max_in_flight = max(
                self._max_in_flight,
                self._in_flight,
            )
        try:
            time.sleep(self._latency_seconds)
        finally:
            with self._lock:
                self._in_flight -= 1

        if attempt > 1:
            with self._lock:
                self._recovered_queries.add(query)
        slug = quote(query, safe="")
        return [
            SearchResult(
                url=f"https://controlled.test/{slug}/{index}",
                title=f"{query} result {index}",
                snippet=f"Controlled evidence for {query}, item {index}.",
                content=(
                    f"This deterministic document answers {query}. "
                    f"It is controlled result {index}."
                ),
                site="controlled.test",
                score=1.0 - index * 0.1,
                provider_rank=index,
                source=self.name,
                raw={"id": f"{slug}-{index}"},
            )
            for index in range(min(top_k, 5))
        ]

    def snapshot_metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls": self._calls,
                "max_in_flight": self._max_in_flight,
                "injected_transient_failures": len(self._injected_queries),
                "recovered_transient_failures": len(
                    self._recovered_queries
                ),
            }


class ControlledScorer(Reranker):
    """Small deterministic scorer used to exercise ranking-pool isolation."""

    name = "controlled-scorer"

    def __init__(self, latency_ms: float = 5.0) -> None:
        self._latency_seconds = max(0.0, latency_ms / 1000)
        self._lock = Lock()
        self._calls = 0
        self._in_flight = 0
        self._max_in_flight = 0

    def score(
        self,
        query: str,
        texts: Sequence[str],
    ) -> list[float]:
        with self._lock:
            self._calls += 1
            self._in_flight += 1
            self._max_in_flight = max(
                self._max_in_flight,
                self._in_flight,
            )
        try:
            time.sleep(self._latency_seconds)
        finally:
            with self._lock:
                self._in_flight -= 1
        return [
            max(0.0, 0.95 - index * 0.05)
            for index, _ in enumerate(texts)
        ]

    def rerank(
        self,
        query: str,
        results: list[SearchResult],
        top_k: int,
    ) -> list[SearchResult]:
        ranked = [item.model_copy(deep=True) for item in results]
        for item, score in zip(
            ranked,
            self.score(
                query,
                [item.text_for_rerank() for item in ranked],
            ),
        ):
            item.rerank_score = score
        return sorted(
            ranked,
            key=lambda item: item.rerank_score or 0.0,
            reverse=True,
        )[:top_k]

    def snapshot_metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls": self._calls,
                "max_in_flight": self._max_in_flight,
            }


@dataclass
class _Runtime:
    service: SearchService
    provider: ControlledSearchProvider
    scorer: ControlledScorer
    resilience: ResilienceManager
    recall_executor: ThreadPoolExecutor
    ranking_executor: ThreadPoolExecutor
    ranking: RankingService
    seed_store: SqliteSearchSeedStore
    recall_workers: int
    ranking_workers: int

    def close(self) -> None:
        self.ranking.close()
        self.recall_executor.shutdown(wait=True, cancel_futures=True)
        self.ranking_executor.shutdown(wait=True, cancel_futures=True)
        self.seed_store.close()


def _build_runtime(
    *,
    provider_latency_ms: float,
    transient_every: int,
    deadline_ms: int,
) -> _Runtime:
    recall_workers = 16
    ranking_workers = 4
    settings = Settings(
        default_top_k=5,
        per_provider_k=5,
        provider_timeout=max(1, math.ceil(deadline_ms / 1000)),
        search_deadline_ms=deadline_ms,
        search_seed_ttl_seconds=60,
        ranking_profile="quality",
        rerank_backend=ControlledScorer.name,
        rerank_threshold_mode="off",
        rewrite_enabled=False,
        openalex_enabled=False,
        openalex_academic_detect=False,
        openalex_query_rewrite=False,
        patent_es_enabled=False,
        patent_detect=False,
        cache_enabled=False,
        executor_max_workers=recall_workers,
        ranking_executor_max_workers=ranking_workers,
        resilience_max_attempts=2,
        resilience_backoff_base_ms=5,
        resilience_backoff_max_ms=5,
        circuit_failure_threshold=100,
        circuit_open_seconds=1,
        mcp_mode="false",
    )
    clock = SystemClock()
    provider = ControlledSearchProvider(
        latency_ms=provider_latency_ms,
        transient_every=transient_every,
    )
    registry = SourceRegistry([provider])
    recall_executor = ThreadPoolExecutor(
        max_workers=recall_workers,
        thread_name_prefix="gate-recall",
    )
    ranking_executor = ThreadPoolExecutor(
        max_workers=ranking_workers,
        thread_name_prefix="gate-ranking",
    )
    resilience = ResilienceManager(
        settings,
        clock,
        random_value=lambda: 0.0,
    )
    scorer = ControlledScorer()
    ranking = RankingService(
        settings,
        scorer,
        lambda *_: scorer,
        ranking_executor,
        clock=clock,
        resilience=resilience,
    )
    discovery = DiscoveryService(
        query_planner=QueryPlanner(settings, resilience=resilience),
        recall=RecallCoordinator(
            settings,
            registry,
            None,
            recall_executor,
            clock=clock.now,
            resilience=resilience,
        ),
        ranking=ranking,
        source_registry=registry,
        clock=clock,
        deadline_ms=deadline_ms,
    )
    seed_store = SqliteSearchSeedStore(":memory:")
    service = SearchService(
        discovery=discovery,
        evidence_assembler=EvidenceAssembler(),
        trust_annotator=TrustAnnotator(registry.snapshot_for),
        answerability=AnswerabilityPolicy(),
        seed_store=seed_store,
        clock=clock,
        deadline_ms=deadline_ms,
        seed_ttl_seconds=settings.search_seed_ttl_seconds,
    )
    return _Runtime(
        service=service,
        provider=provider,
        scorer=scorer,
        resilience=resilience,
        recall_executor=recall_executor,
        ranking_executor=ranking_executor,
        ranking=ranking,
        seed_store=seed_store,
        recall_workers=recall_workers,
        ranking_workers=ranking_workers,
    )


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return float(ordered[rank - 1])


def run_level(
    concurrency: int,
    *,
    requests_per_worker: int,
    provider_latency_ms: float,
    transient_every: int,
    deadline_ms: int,
) -> dict[str, Any]:
    if concurrency <= 0 or requests_per_worker <= 0:
        raise ValueError("concurrency and requests_per_worker must be positive")
    request_count = concurrency * requests_per_worker
    runtime = _build_runtime(
        provider_latency_ms=provider_latency_ms,
        transient_every=transient_every,
        deadline_ms=deadline_ms,
    )
    start_barrier = Barrier(concurrency)

    def invoke(index: int) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if index < concurrency:
                start_barrier.wait(timeout=30)
            response = runtime.service.execute(SearchCommand(
                query=f"concurrency gate {concurrency}-{index}",
                limit=5,
                source_types=("web",),
            ))
            return {
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "exception": None,
                "status": response.status,
                "assessment": response.retrieval_assessment.status,
                "failure_codes": [
                    failure.code for failure in response.failures
                ],
            }
        except Exception as exc:
            return {
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "exception": f"{type(exc).__name__}: {exc}",
                "status": None,
                "assessment": None,
                "failure_codes": [],
            }

    wall_started = time.perf_counter()
    try:
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix=f"gate-client-{concurrency}",
        ) as clients:
            futures = [
                clients.submit(invoke, index)
                for index in range(request_count)
            ]
            rows = [future.result() for future in as_completed(futures)]
        wall_seconds = max(1e-9, time.perf_counter() - wall_started)
        provider = runtime.provider.snapshot_metrics()
        scorer = runtime.scorer.snapshot_metrics()
        resilience = runtime.resilience.snapshot()
    finally:
        runtime.close()

    latencies = [float(row["elapsed_ms"]) for row in rows]
    response_count = sum(row["exception"] is None for row in rows)
    complete_count = sum(row["status"] == "complete" for row in rows)
    usable_count = sum(row["assessment"] == "usable" for row in rows)
    deadline_failures = sum(
        "SEARCH_DEADLINE_EXCEEDED" in row["failure_codes"]
        for row in rows
    )
    injected = provider["injected_transient_failures"]
    recovered = provider["recovered_transient_failures"]
    dependency = resilience["dependencies"].get(
        ControlledSearchProvider.name,
        {},
    )
    return {
        "concurrency": concurrency,
        "request_count": request_count,
        "requests_per_worker": requests_per_worker,
        "success_rate": response_count / request_count,
        "complete_rate": complete_count / request_count,
        "usable_rate": usable_count / request_count,
        "exception_rate": (request_count - response_count) / request_count,
        "deadline_failure_rate": deadline_failures / request_count,
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "p99": round(_percentile(latencies, 0.99), 3),
            "max": round(max(latencies, default=0.0), 3),
        },
        "throughput_rps": round(request_count / wall_seconds, 3),
        "wall_seconds": round(wall_seconds, 3),
        "provider_calls": provider["calls"],
        "provider_max_in_flight": provider["max_in_flight"],
        "scorer_calls": scorer["calls"],
        "scorer_max_in_flight": scorer["max_in_flight"],
        "retry_count": int(dependency.get("retries", 0)),
        "injected_transient_failures": injected,
        "recovered_transient_failures": recovered,
        "retry_recovery_rate": recovered / injected if injected else 1.0,
        "isolation": {
            "recall_workers": runtime.recall_workers,
            "ranking_workers": runtime.ranking_workers,
            "recall_pool_bound_respected": (
                provider["max_in_flight"] <= runtime.recall_workers
            ),
            "ranking_pool_bound_respected": (
                scorer["max_in_flight"] <= runtime.ranking_workers
            ),
        },
        "exceptions": sorted({
            str(row["exception"])
            for row in rows
            if row["exception"] is not None
        }),
    }


_MINIMUMS = {
    "min_success_rate": "success_rate",
    "min_complete_rate": "complete_rate",
    "min_usable_rate": "usable_rate",
    "min_retry_recovery_rate": "retry_recovery_rate",
    "min_throughput_rps": "throughput_rps",
    "min_provider_parallelism": "provider_max_in_flight",
    "min_scorer_parallelism": "scorer_max_in_flight",
}
_MAXIMUMS = {
    "max_exception_rate": "exception_rate",
    "max_deadline_failure_rate": "deadline_failure_rate",
    "max_p95_ms": "latency_ms.p95",
    "max_provider_parallelism": "provider_max_in_flight",
    "max_scorer_parallelism": "scorer_max_in_flight",
}


def _metric(result: dict[str, Any], path: str) -> float:
    value: Any = result
    for part in path.split("."):
        value = value[part]
    return float(value)


def compare_level(
    result: dict[str, Any],
    threshold: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    for name, path in _MINIMUMS.items():
        expected = float(threshold[name])
        observed = _metric(result, path)
        if observed + 1e-12 < expected:
            failures.append(
                f"{path}={observed:.3f} below minimum={expected:.3f}"
            )
    for name, path in _MAXIMUMS.items():
        expected = float(threshold[name])
        observed = _metric(result, path)
        if observed - 1e-12 > expected:
            failures.append(
                f"{path}={observed:.3f} above maximum={expected:.3f}"
            )
    if not result["isolation"]["recall_pool_bound_respected"]:
        failures.append("recall pool concurrency bound was exceeded")
    if not result["isolation"]["ranking_pool_bound_respected"]:
        failures.append("ranking pool concurrency bound was exceeded")
    if result["retry_count"] != result["injected_transient_failures"]:
        failures.append(
            "retry counter does not match injected transient failures: "
            f"{result['retry_count']} != "
            f"{result['injected_transient_failures']}"
        )
    return failures


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_gate(
    *,
    levels: Sequence[int] = DEFAULT_LEVELS,
    requests_per_worker: int = 2,
    thresholds_path: Path = DEFAULT_THRESHOLDS,
    report_path: Path | None = DEFAULT_REPORT,
    provider_latency_ms: float = 20.0,
    transient_every: int = 10,
    deadline_ms: int = 1500,
) -> dict[str, Any]:
    thresholds = json.loads(thresholds_path.read_text(encoding="utf-8"))
    if thresholds.get("schema_version") != "concurrency-thresholds.v1":
        raise ValueError("unsupported concurrency threshold version")
    results: dict[str, Any] = {}
    failures: list[str] = []
    for level in levels:
        key = str(int(level))
        if key not in thresholds.get("levels", {}):
            failures.append(f"missing thresholds for concurrency={key}")
            continue
        result = run_level(
            int(level),
            requests_per_worker=requests_per_worker,
            provider_latency_ms=provider_latency_ms,
            transient_every=transient_every,
            deadline_ms=deadline_ms,
        )
        result["failures"] = compare_level(
            result,
            thresholds["levels"][key],
        )
        results[key] = result
        failures.extend(
            f"concurrency={key}: {failure}"
            for failure in result["failures"]
        )
    report = {
        "schema_version": "concurrency-report.v1",
        "runner_version": RUNNER_VERSION,
        "status": "failed" if failures else "passed",
        "thresholds_sha256": _sha256(thresholds_path),
        "workload": {
            "levels": [int(level) for level in levels],
            "requests_per_worker": requests_per_worker,
            "provider_latency_ms": provider_latency_ms,
            "transient_every": transient_every,
            "deadline_ms": deadline_ms,
            "external_network": False,
        },
        "levels": results,
        "failures": failures,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if failures:
        raise RuntimeError(
            "concurrency gate failed: " + "; ".join(failures)
        )
    return report


def parse_levels(value: str) -> tuple[int, ...]:
    levels = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not levels or any(level <= 0 for level in levels):
        raise argparse.ArgumentTypeError("levels must be positive integers")
    return levels


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", type=parse_levels, default=DEFAULT_LEVELS)
    parser.add_argument("--requests-per-worker", type=int, default=2)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--provider-latency-ms", type=float, default=20.0)
    parser.add_argument("--transient-every", type=int, default=10)
    parser.add_argument("--deadline-ms", type=int, default=1500)
    args = parser.parse_args()
    report = run_gate(
        levels=args.levels,
        requests_per_worker=args.requests_per_worker,
        thresholds_path=args.thresholds,
        report_path=args.report,
        provider_latency_ms=args.provider_latency_ms,
        transient_every=args.transient_every,
        deadline_ms=args.deadline_ms,
    )
    summary = {
        level: {
            "p95_ms": result["latency_ms"]["p95"],
            "throughput_rps": result["throughput_rps"],
            "success_rate": result["success_rate"],
            "retry_recovery_rate": result["retry_recovery_rate"],
        }
        for level, result in report["levels"].items()
    }
    print(json.dumps({
        "status": report["status"],
        "levels": summary,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
