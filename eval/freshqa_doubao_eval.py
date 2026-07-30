"""Compare Doubao Search MCP against the pinned Chukonu FreshQA baseline."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.doubao_search_client import DOUBAO_MCP_REVISION, DoubaoMcpSearchClient
from eval.freshqa_eval import (
    ANSWER_SYSTEM,
    OFFICIAL_REPO_COMMIT,
    SNAPSHOT_DATE,
    ChatClient,
    _answers,
    _atomic_json,
    _contains_reference,
    _evidence_prompt,
    _judge,
    _load_rows,
)
from eval.freshqa_reporting import render_report, summarize
from src.config import Settings


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evaluate_engine(
    row: dict[str, str],
    baseline_row: dict[str, Any],
    answer_client: ChatClient,
    judge_client: ChatClient,
    search_client: DoubaoMcpSearchClient,
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
        "status": search_response["status"],
        "evidence_count": len(search_response["evidence"]),
        "sources": ["doubao"],
        "failures": search_response["failures"],
        "retrieval_assessment": search_response["retrieval_assessment"],
        "evidence": [
            {
                "source": item["source"],
                "title": item["title"],
                "url": item["url"],
                "passage": item["passage"]["text"][:2000],
                "metadata": item.get("metadata", {}),
            }
            for item in search_response["evidence"]
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
            mismatches[f"question:{row['id']}"] = ("missing/different", "expected")
    if mismatches:
        raise ValueError(f"Baseline is not comparable: {mismatches}")
    return baseline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-cache", default=f"eval/cache/freshqa_{SNAPSHOT_DATE}.csv"
    )
    parser.add_argument("--split", choices=("TEST", "DEV"), default="TEST")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--search-limit", type=int, default=8)
    parser.add_argument("--search-timeout", type=float, default=45)
    parser.add_argument("--model-timeout", type=float, default=90)
    parser.add_argument("--evidence-chars", type=int, default=12_000)
    parser.add_argument(
        "--answer-model", default="Qwen/Qwen3-30B-A3B-Instruct-2507"
    )
    parser.add_argument("--judge-model", default="deepseek-ai/DeepSeek-V3.2")
    parser.add_argument("--evaluation-date", default="2026-07-29")
    parser.add_argument(
        "--baseline-details", default="eval/freshqa_chukonu_details.json"
    )
    parser.add_argument(
        "--details-path", default="eval/freshqa_doubao_details.json"
    )
    parser.add_argument("--report-path", default="eval/freshqa_doubao_report.md")
    parser.add_argument("--uvx-path", default=".venv/bin/uvx")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.siliconflow_api_key:
        raise SystemExit("SILICONFLOW_API_KEY is required")
    if not 1 <= args.search_limit <= 50:
        raise SystemExit("--search-limit must be between 1 and 50")

    all_rows, dataset_hash = _load_rows(Path(args.dataset_cache), args.split)
    rng = random.Random(args.seed)
    selected = (
        rng.sample(all_rows, args.sample_size)
        if 0 < args.sample_size < len(all_rows)
        else list(all_rows)
    )
    selected.sort(key=lambda row: int(row["id"]))
    baseline_path = Path(args.baseline_details)
    baseline = _validated_baseline(baseline_path, selected, dataset_hash, args)
    config = {
        "split": args.split,
        "sample_size": len(selected),
        "seed": args.seed,
        "search_backend": "doubao-search-mcp",
        "search_limit": args.search_limit,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "evaluation_date": args.evaluation_date,
        "evidence_chars": args.evidence_chars,
        "engine_label": "Doubao Search",
        "official_repo_commit": OFFICIAL_REPO_COMMIT,
        "doubao_mcp_revision": DOUBAO_MCP_REVISION,
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
        "results": {},
    }
    if details_path.exists():
        cached = json.loads(details_path.read_text(encoding="utf-8"))
        if cached.get("config_fingerprint") != fingerprint:
            raise SystemExit(
                "Existing details use a different config; choose another output path"
            )
        details = cached

    answer_client = ChatClient(settings, args.answer_model, args.model_timeout)
    judge_client = ChatClient(settings, args.judge_model, args.model_timeout)
    pending = [
        row
        for row in selected
        if row["id"] not in details["results"]
        or "error" in details["results"][row["id"]]
    ]
    completed = len(selected) - len(pending)
    print(
        f"Doubao FreshQA selected={len(selected)} resumed={completed} "
        f"pending={len(pending)}",
        flush=True,
    )
    write_lock = threading.Lock()
    with DoubaoMcpSearchClient(
        uvx_path=args.uvx_path,
        timeout=args.search_timeout,
        limit=args.search_limit,
    ) as search_client:
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
                    details["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
                    _atomic_json(details_path, details)
                state = "ok" if "error" not in result else "ERROR"
                print(
                    f"[{completed:03}/{len(selected):03}] id={row['id']} "
                    f"{state} {row['question'][:58]}",
                    flush=True,
                )

    errors = [row for row in details["results"].values() if "error" in row]
    if errors:
        raise SystemExit(f"{len(errors)} items failed; rerun to resume")
    ordered = [details["results"][row["id"]] for row in selected]
    details["summary"] = summarize(ordered, args.seed)
    details["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(details_path, details)
    Path(args.report_path).write_text(
        render_report(details, snapshot_date=SNAPSHOT_DATE), encoding="utf-8"
    )
    print(f"report={args.report_path} details={args.details_path}", flush=True)


if __name__ == "__main__":
    main()
