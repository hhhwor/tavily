"""FreshQA evaluation for the current Chukonu default multi-source route."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from eval.freshqa_eval import (
    ANSWER_SYSTEM,
    OFFICIAL_REPO_COMMIT,
    SNAPSHOT_DATE,
    ChatClient,
    SearchClient,
    _answers,
    _atomic_json,
    _contains_reference,
    _evidence_prompt,
    _judge,
    _load_rows,
)
from eval.freshqa_reporting import _paired_bootstrap, render_report, summarize
from src.config import Settings


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_health(
    search_url: str,
    expected_providers: tuple[str, ...],
    timeout: float,
) -> dict[str, Any]:
    health_url = search_url.rsplit("/", 1)[0] + "/health"
    response = requests.get(health_url, timeout=min(timeout, 10))
    response.raise_for_status()
    health = response.json()
    actual = tuple(health.get("providers") or ())
    if actual != expected_providers:
        raise ValueError(
            f"Provider mismatch: expected {expected_providers}, got {actual}"
        )
    return {
        "url": health_url,
        "providers": list(actual),
        "reranker": health.get("reranker"),
    }


def _validated_baseline(
    path: Path,
    selected: list[dict[str, str]],
    dataset_hash: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    baseline = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "split": args.split,
        "sample_size": len(selected),
        "seed": args.seed,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "evaluation_date": args.evaluation_date,
    }
    mismatches = {
        key: (baseline.get("config", {}).get(key), value)
        for key, value in expected.items()
        if baseline.get("config", {}).get(key) != value
    }
    if baseline.get("dataset_sha256") != dataset_hash:
        mismatches["dataset_sha256"] = (
            baseline.get("dataset_sha256"),
            dataset_hash,
        )
    for row in selected:
        saved = baseline.get("results", {}).get(row["id"])
        if not saved or saved.get("question") != row["question"]:
            mismatches[f"question:{row['id']}"] = (
                "missing/different",
                "expected",
            )
    if mismatches:
        raise ValueError(f"Baseline is not comparable: {mismatches}")
    return baseline


def _evaluate_engine(
    row: dict[str, str],
    baseline_row: dict[str, Any],
    answer_client: ChatClient,
    judge_client: ChatClient,
    search_client: SearchClient,
    evaluation_date: str,
    evidence_chars: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    search_response = search_client.search(row["question"])
    search_ms = round((time.perf_counter() - started) * 1000, 1)
    evidence = _evidence_prompt(search_response, evidence_chars)
    system = ANSWER_SYSTEM.format(evaluation_date=evaluation_date)
    started = time.perf_counter()
    answer, usage = answer_client.call(
        system,
        f"Question: {row['question']}\n\nRetrieved evidence:\n{evidence}",
        max_tokens=128,
    )
    engine = {
        "answer": answer,
        "search_ms": search_ms,
        "answer_ms": round((time.perf_counter() - started) * 1000, 1),
        "usage": usage,
        "contains_reference": _contains_reference(answer, _answers(row)),
        "status": search_response.get("status"),
        "evidence_count": len(search_response.get("evidence", [])),
        "sources": sorted({
            item.get("source", "")
            for item in search_response.get("evidence", [])
        }),
        "failures": search_response.get("failures", []),
        "retrieval_assessment": search_response.get(
            "retrieval_assessment",
            {},
        ),
        "evidence": [
            {
                "source": item.get("source"),
                "title": item.get("title"),
                "url": item.get("url"),
                "passage": (
                    (item.get("passage") or {}).get("text") or ""
                )[:2000],
            }
            for item in search_response.get("evidence", [])
        ],
    }
    started = time.perf_counter()
    judgment, judge_usage = _judge(judge_client, row, answer)
    engine["judgment"] = judgment
    engine["judge_usage"] = judge_usage
    engine["judge_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {
        "id": row["id"],
        "question": row["question"],
        "metadata": {
            key: row[key]
            for key in (
                "effective_year",
                "next_review",
                "false_premise",
                "num_hops",
                "fact_type",
            )
        },
        "answers": _answers(row),
        "baseline": copy.deepcopy(baseline_row["baseline"]),
        "engine": engine,
    }


def _source_mix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_labels: Counter[str] = Counter()
    provider_credits: Counter[str] = Counter()
    queries_by_provider: Counter[str] = Counter()
    for row in rows:
        providers_in_query: set[str] = set()
        for evidence in row["engine"]["evidence"]:
            source = str(evidence.get("source") or "unknown")
            evidence_labels[source] += 1
            providers = {item for item in source.split("+") if item}
            provider_credits.update(providers)
            providers_in_query.update(providers)
        queries_by_provider.update(providers_in_query)
    return {
        "evidence_labels": dict(evidence_labels),
        "provider_evidence_credits": dict(provider_credits),
        "queries_with_provider": dict(queries_by_provider),
    }


def _comparison(
    rows: list[dict[str, Any]],
    path: Path,
    label: str,
    dataset_hash: str,
    seed: int,
) -> dict[str, Any]:
    prior = json.loads(path.read_text(encoding="utf-8"))
    if prior.get("dataset_sha256") != dataset_hash:
        raise ValueError(f"{label} dataset does not match")
    current = {row["id"]: row for row in rows}
    prior_results = prior.get("results", {})
    if set(current) != set(prior_results):
        raise ValueError(f"{label} sample ids do not match")
    output: dict[str, Any] = {"label": label, "path": str(path)}
    for metric in ("strict", "relaxed"):
        left = [
            int(current[row_id]["engine"]["judgment"][metric])
            for row_id in sorted(current, key=int)
        ]
        right = [
            int(prior_results[row_id]["engine"]["judgment"][metric])
            for row_id in sorted(current, key=int)
        ]
        output[metric] = {
            "current": statistics.fmean(left),
            "prior": statistics.fmean(right),
            "delta": statistics.fmean(left) - statistics.fmean(right),
            "delta_ci95": _paired_bootstrap(left, right, seed),
            "wins": sum(a > b for a, b in zip(left, right)),
            "ties": sum(a == b for a, b in zip(left, right)),
            "losses": sum(a < b for a, b in zip(left, right)),
        }
    output["search"] = prior["summary"]["search"]
    return output


def _extended_report(details: dict[str, Any]) -> str:
    report = render_report(details, snapshot_date=SNAPSHOT_DATE)
    source_mix = details["summary"]["source_mix"]
    lines = [
        "",
        "## 多源组成",
        "",
        f"- 运行时默认源：`{details['config']['providers']}`",
        f"- evidence 来源标签计数：`{source_mix['evidence_labels']}`",
        f"- 拆分合并来源后的 evidence 贡献：`{source_mix['provider_evidence_credits']}`",
        f"- 至少出现一次该 provider 的查询数：`{source_mix['queries_with_provider']}`",
        "",
        "## 与既有同样本结果对比",
        "",
        "| 对照 | 口径 | 对照结果 | 新多源 | Δ（95% CI） | 胜/平/负 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for comparison in details["summary"]["comparisons"]:
        for metric, label in (("strict", "严格"), ("relaxed", "宽松")):
            item = comparison[metric]
            lines.append(
                f"| {comparison['label']} | {label} | "
                f"{item['prior']:.1%} | {item['current']:.1%} | "
                f"{item['delta']:+.1%} "
                f"([{item['delta_ci95'][0]:+.1%}, "
                f"{item['delta_ci95'][1]:+.1%}]) | "
                f"{item['wins']}/{item['ties']}/{item['losses']} |"
            )
    current_search = details["summary"]["search"]
    lines += [
        "",
        "## 运行对比",
        "",
        "| 配置 | complete | provider failure | evidence 均值 | P50 | P95 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for comparison in details["summary"]["comparisons"]:
        item = comparison["search"]
        lines.append(
            f"| {comparison['label']} | {item['complete_rate']:.1%} | "
            f"{item['provider_failure_rate']:.1%} | "
            f"{item['avg_evidence']:.2f} | "
            f"{item['p50_ms']:.0f} ms | {item['p95_ms']:.0f} ms |"
        )
    lines.append(
        f"| 新多源 | {current_search['complete_rate']:.1%} | "
        f"{current_search['provider_failure_rate']:.1%} | "
        f"{current_search['avg_evidence']:.2f} | "
        f"{current_search['p50_ms']:.0f} ms | "
        f"{current_search['p95_ms']:.0f} ms |"
    )
    return report + "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-cache", default=f"eval/cache/freshqa_{SNAPSHOT_DATE}.csv"
    )
    parser.add_argument("--split", choices=("TEST", "DEV"), default="TEST")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--search-url", default="http://127.0.0.1:8000/search")
    parser.add_argument("--search-limit", type=int, default=8)
    parser.add_argument("--search-timeout", type=float, default=45)
    parser.add_argument("--model-timeout", type=float, default=90)
    parser.add_argument("--evidence-chars", type=int, default=12_000)
    parser.add_argument(
        "--answer-model", default="Qwen/Qwen3-30B-A3B-Instruct-2507"
    )
    parser.add_argument("--judge-model", default="deepseek-ai/DeepSeek-V3.2")
    parser.add_argument("--evaluation-date", default="2026-07-29")
    parser.add_argument("--expected-providers", default="tencent,baidu,doubao")
    parser.add_argument(
        "--baseline-details", default="eval/freshqa_chukonu_details.json"
    )
    parser.add_argument(
        "--prior-chukonu-details", default="eval/freshqa_chukonu_details.json"
    )
    parser.add_argument(
        "--doubao-details", default="eval/freshqa_doubao_details.json"
    )
    parser.add_argument(
        "--details-path", default="eval/freshqa_multisource_details.json"
    )
    parser.add_argument(
        "--report-path", default="eval/freshqa_multisource_report.md"
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.siliconflow_api_key:
        raise SystemExit("SILICONFLOW_API_KEY is required")
    if not settings.auth_tokens:
        raise SystemExit("API_AUTH_TOKEN is required")
    expected_providers = tuple(
        item.strip()
        for item in args.expected_providers.split(",")
        if item.strip()
    )
    health = _validate_health(
        args.search_url,
        expected_providers,
        args.search_timeout,
    )
    all_rows, dataset_hash = _load_rows(
        Path(args.dataset_cache),
        args.split,
    )
    rng = random.Random(args.seed)
    selected = (
        rng.sample(all_rows, args.sample_size)
        if 0 < args.sample_size < len(all_rows)
        else list(all_rows)
    )
    selected.sort(key=lambda row: int(row["id"]))
    baseline_path = Path(args.baseline_details)
    baseline = _validated_baseline(
        baseline_path,
        selected,
        dataset_hash,
        args,
    )
    config = {
        "split": args.split,
        "sample_size": len(selected),
        "seed": args.seed,
        "search_url": args.search_url,
        "search_limit": args.search_limit,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "evaluation_date": args.evaluation_date,
        "evidence_chars": args.evidence_chars,
        "engine_label": "Chukonu 新多源",
        "official_repo_commit": OFFICIAL_REPO_COMMIT,
        "providers": list(expected_providers),
        "reranker": health["reranker"],
        "baseline_config_fingerprint": baseline["config_fingerprint"],
        "baseline_details_sha256": _sha256(baseline_path),
    }
    fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).hexdigest()
    details_path = Path(args.details_path)
    details: dict[str, Any] = {
        "config": config,
        "config_fingerprint": fingerprint,
        "dataset_url": baseline["dataset_url"],
        "dataset_sha256": dataset_hash,
        "runtime_health": health,
        "results": {},
    }
    if details_path.exists():
        cached = json.loads(details_path.read_text(encoding="utf-8"))
        if cached.get("config_fingerprint") != fingerprint:
            raise SystemExit(
                "Existing details use a different config; choose another output path"
            )
        details = cached

    answer_client = ChatClient(
        settings,
        args.answer_model,
        args.model_timeout,
    )
    judge_client = ChatClient(
        settings,
        args.judge_model,
        args.model_timeout,
    )
    search_client = SearchClient(
        settings,
        args.search_url,
        args.search_timeout,
        args.search_limit,
    )
    pending = [
        row
        for row in selected
        if row["id"] not in details["results"]
        or "error" in details["results"][row["id"]]
    ]
    completed = len(selected) - len(pending)
    print(
        f"Multi-source FreshQA selected={len(selected)} resumed={completed} "
        f"pending={len(pending)} providers={expected_providers}",
        flush=True,
    )
    write_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _evaluate_engine,
                row,
                baseline["results"][row["id"]],
                answer_client,
                judge_client,
                search_client,
                args.evaluation_date,
                args.evidence_chars,
            ): row
            for row in pending
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "id": row["id"],
                    "question": row["question"],
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                }
            with write_lock:
                details["results"][row["id"]] = result
                completed += 1
                details["generated_at_utc"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                _atomic_json(details_path, details)
            state = "ok" if "error" not in result else "ERROR"
            print(
                f"[{completed:03}/{len(selected):03}] id={row['id']} "
                f"{state} {row['question'][:58]}",
                flush=True,
            )

    errors = [
        row
        for row in details["results"].values()
        if "error" in row
    ]
    if errors:
        raise SystemExit(f"{len(errors)} items failed; rerun to resume")
    ordered = [details["results"][row["id"]] for row in selected]
    summary = summarize(ordered, args.seed)
    summary["source_mix"] = _source_mix(ordered)
    summary["comparisons"] = [
        _comparison(
            ordered,
            Path(args.prior_chukonu_details),
            "旧 Chukonu",
            dataset_hash,
            args.seed,
        ),
        _comparison(
            ordered,
            Path(args.doubao_details),
            "Doubao 单源",
            dataset_hash,
            args.seed,
        ),
    ]
    details["summary"] = summary
    details["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(details_path, details)
    Path(args.report_path).write_text(
        _extended_report(details),
        encoding="utf-8",
    )
    print(
        f"report={args.report_path} details={args.details_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
