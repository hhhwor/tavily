"""Paired FreshQA comparison: current three-source vs Aliyun four-source."""
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
from typing import Any, Callable

import requests

from eval.freshqa_eval import (
    ANSWER_SYSTEM,
    JUDGE_SYSTEM,
    OFFICIAL_REPO_COMMIT,
    SNAPSHOT_DATE,
    ChatClient,
    SearchClient,
    _answers,
    _atomic_json,
    _contains_reference,
    _evidence_prompt,
    _load_rows,
    _parse_judgment,
)
from eval.freshqa_aliyun_paired_reporting import (
    SYSTEMS,
    render_report,
    summarize,
)
from src.config import Settings


def _selected_rows(
    rows: list[dict[str, str]],
    sample_size: int,
    seed: int,
    max_items: int,
) -> list[dict[str, str]]:
    rng = random.Random(seed)
    selected = (
        rng.sample(rows, sample_size)
        if 0 < sample_size < len(rows)
        else list(rows)
    )
    selected.sort(key=lambda row: int(row["id"]))
    return selected[:max_items] if max_items > 0 else selected


def _metadata(row: dict[str, str]) -> dict[str, str]:
    keys = (
        "effective_year",
        "next_review",
        "false_premise",
        "num_hops",
        "fact_type",
    )
    return {key: row[key] for key in keys}


def _validate_health(
    search_url: str,
    expected: tuple[str, ...],
    timeout: float,
) -> dict[str, Any]:
    health_url = search_url.rsplit("/", 1)[0] + "/health"
    response = requests.get(health_url, timeout=min(timeout, 10))
    response.raise_for_status()
    health = response.json()
    actual = tuple(health.get("providers") or ())
    if actual != expected:
        raise ValueError(
            f"{health_url}: expected providers {expected}, got {actual}"
        )
    return {
        "url": health_url,
        "providers": list(actual),
        "reranker": health.get("reranker"),
        "cache": health.get("cache"),
    }


def _retry(call: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], int]:
    last: dict[str, Any] = {}
    for attempt in range(3):
        try:
            last = call()
            if last.get("status") in {"ok", "complete", "partial"}:
                return last, attempt
        except requests.RequestException as exc:
            last = {
                "status": "failed",
                "failures": [{
                    "source": "endpoint",
                    "code": type(exc).__name__,
                }],
            }
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return last, 2


def _compact_search(
    response: dict[str, Any],
    elapsed_ms: float,
    retries: int,
) -> dict[str, Any]:
    evidence = [
        {
            "source": item.get("source"),
            "title": item.get("title"),
            "url": item.get("url"),
            "passage": ((item.get("passage") or {}).get("text") or "")[:2000],
        }
        for item in response.get("evidence", [])
    ]
    return {
        "status": response.get("status", "failed"),
        "search_ms": round(elapsed_ms, 1),
        "retries": retries,
        "evidence_count": len(evidence),
        "sources": sorted({
            str(item.get("source") or "") for item in evidence
        }),
        "failures": response.get("failures", []),
        "retrieval_assessment": response.get("retrieval_assessment", {}),
        "evidence": evidence,
    }


def _timed_search(
    client: SearchClient,
    query: str,
) -> tuple[dict[str, Any], float, int]:
    started = time.perf_counter()
    response, retries = _retry(lambda: client.search(query))
    elapsed_ms = (time.perf_counter() - started) * 1000
    return response, elapsed_ms, retries


def _retrieve_pair(
    row: dict[str, str],
    settings: Settings,
    args: argparse.Namespace,
) -> dict[str, Any]:
    clients = {
        "three_source": SearchClient(
            settings,
            args.three_source_url,
            args.search_timeout,
            args.search_limit,
        ),
        "four_source": SearchClient(
            settings,
            args.four_source_url,
            args.search_timeout,
            args.search_limit,
        ),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(_timed_search, client, row["question"])
            for name, client in clients.items()
        }
        output = {}
        for name, future in futures.items():
            response, elapsed_ms, retries = future.result()
            output[name] = _compact_search(response, elapsed_ms, retries)
    return output


def _as_response(retrieval: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": retrieval["status"],
        "failures": retrieval["failures"],
        "retrieval_assessment": retrieval["retrieval_assessment"],
        "evidence": [
            {
                "source": item["source"],
                "title": item["title"],
                "url": item["url"],
                "passage": {"text": item["passage"]},
            }
            for item in retrieval["evidence"]
        ],
    }


def _judge_at_date(
    client: ChatClient,
    row: dict[str, str],
    answer: str,
    evaluation_date: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    payload = {
        "question": row["question"],
        "correct_answers": _answers(row),
        "false_premise": row["false_premise"],
        "evaluation_date": evaluation_date,
        "model_response": answer,
    }
    text, usage = client.call(
        JUDGE_SYSTEM,
        json.dumps(payload, ensure_ascii=False),
        max_tokens=320,
        json_output=True,
    )
    return _parse_judgment(text), usage


def _answer_row(
    row: dict[str, str],
    cached: dict[str, Any],
    settings: Settings,
    args: argparse.Namespace,
) -> dict[str, Any]:
    result = copy.deepcopy(cached)
    result.pop("answer_error", None)
    answer_client = ChatClient(settings, args.answer_model, args.model_timeout)
    judge_client = ChatClient(settings, args.judge_model, args.model_timeout)
    system_prompt = ANSWER_SYSTEM.format(
        evaluation_date=args.evaluation_date
    )

    def answer(name: str) -> tuple[str, dict[str, Any]]:
        evidence = _evidence_prompt(
            _as_response(result["retrieval"][name]),
            args.evidence_chars,
        )
        started = time.perf_counter()
        text, usage = answer_client.call(
            system_prompt,
            f"Question: {row['question']}\n\nRetrieved evidence:\n"
            f"{evidence}",
            max_tokens=128,
        )
        return text, {
            "answer_ms": round((time.perf_counter() - started) * 1000, 1),
            "usage": usage,
        }

    systems: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(answer, name)
            for name in SYSTEMS
        }
        for name, future in futures.items():
            text, metadata = future.result()
            systems[name] = {"answer": text, **metadata}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(
                _judge_at_date,
                judge_client,
                row,
                systems[name]["answer"],
                args.evaluation_date,
            )
            for name in SYSTEMS
        }
        for name, future in futures.items():
            judgment, usage = future.result()
            systems[name]["judgment"] = judgment
            systems[name]["judge_usage"] = usage
            systems[name]["contains_reference"] = _contains_reference(
                systems[name]["answer"],
                _answers(row),
            )
    result["systems"] = systems
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-cache", default=f"eval/cache/freshqa_{SNAPSHOT_DATE}.csv")
    parser.add_argument("--split", choices=("TEST", "DEV"), default="TEST")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--retrieval-workers", type=int, default=2)
    parser.add_argument("--answer-workers", type=int, default=1)
    parser.add_argument("--three-source-url", default="http://127.0.0.1:8010/search")
    parser.add_argument("--four-source-url", default="http://127.0.0.1:8011/search")
    parser.add_argument("--search-limit", type=int, default=8)
    parser.add_argument("--search-timeout", type=float, default=60)
    parser.add_argument("--model-timeout", type=float, default=90)
    parser.add_argument("--evidence-chars", type=int, default=12_000)
    parser.add_argument("--aliyun-unit-cost-rmb", type=float, default=0.042)
    parser.add_argument("--answer-model", default="Qwen/Qwen3-30B-A3B-Instruct-2507")
    parser.add_argument("--judge-model", default="deepseek-ai/DeepSeek-V3.2")
    parser.add_argument("--evaluation-date", default="2026-07-29")
    parser.add_argument("--details-path", default="eval/freshqa_aliyun_paired_details.json")
    parser.add_argument("--report-path", default="eval/freshqa_aliyun_paired_report.md")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.siliconflow_api_key:
        raise SystemExit("SILICONFLOW_API_KEY is required")
    if not settings.auth_tokens:
        raise SystemExit("API_AUTH_TOKEN is required")
    if not settings.aliyun_access_key_id:
        raise SystemExit("Aliyun AccessKey is required")

    health = {
        "three_source": _validate_health(
            args.three_source_url,
            ("tencent", "baidu", "doubao"),
            args.search_timeout,
        ),
        "four_source": _validate_health(
            args.four_source_url,
            ("tencent", "baidu", "doubao", "aliyun"),
            args.search_timeout,
        ),
    }
    all_rows, dataset_hash = _load_rows(
        Path(args.dataset_cache),
        args.split,
    )
    selected = _selected_rows(
        all_rows,
        args.sample_size,
        args.seed,
        args.max_items,
    )
    config = {
        "split": args.split,
        "sample_size": len(selected),
        "source_sample_size": args.sample_size,
        "seed": args.seed,
        "three_source_url": args.three_source_url,
        "four_source_url": args.four_source_url,
        "three_source_providers": health["three_source"]["providers"],
        "four_source_providers": health["four_source"]["providers"],
        "reranker": health["three_source"]["reranker"],
        "search_limit": args.search_limit,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "evaluation_date": args.evaluation_date,
        "evidence_chars": args.evidence_chars,
        "aliyun_search_type": "pro",
        "aliyun_region": "global",
        "aliyun_unit_cost_rmb": args.aliyun_unit_cost_rmb,
        "official_repo_commit": OFFICIAL_REPO_COMMIT,
    }
    fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).hexdigest()
    details_path = Path(args.details_path)
    details: dict[str, Any] = {
        "config": config,
        "config_fingerprint": fingerprint,
        "dataset_sha256": dataset_hash,
        "health": health,
        "results": {},
    }
    if details_path.exists():
        saved = json.loads(details_path.read_text(encoding="utf-8"))
        if saved.get("config_fingerprint") != fingerprint:
            raise SystemExit("Existing details use a different config")
        details = saved
    for row in selected:
        details["results"].setdefault(
            row["id"],
            {
                "id": row["id"],
                "question": row["question"],
                "metadata": _metadata(row),
                "answers": _answers(row),
            },
        )
    _atomic_json(details_path, details)

    pending = [
        row for row in selected
        if "retrieval" not in details["results"][row["id"]]
        or details["results"][row["id"]]["retrieval"].get("fatal_error")
    ]
    print(
        f"retrieval selected={len(selected)} pending={len(pending)}",
        flush=True,
    )
    write_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.retrieval_workers) as pool:
        futures = {
            pool.submit(_retrieve_pair, row, settings, args): row
            for row in pending
        }
        completed = len(selected) - len(pending)
        for future in as_completed(futures):
            row = futures[future]
            try:
                retrieval = future.result()
            except Exception as exc:
                retrieval = {
                    "fatal_error": f"{type(exc).__name__}: {str(exc)[:300]}"
                }
            with write_lock:
                details["results"][row["id"]]["retrieval"] = retrieval
                completed += 1
                _atomic_json(details_path, details)
                print(
                    f"retrieval {completed}/{len(selected)} id={row['id']}",
                    flush=True,
                )

    pending_answers = [
        row for row in selected
        if "fatal_error"
        not in details["results"][row["id"]].get("retrieval", {})
        and (
            "systems" not in details["results"][row["id"]]
            or details["results"][row["id"]].get("answer_error")
        )
    ]
    print(
        f"answers selected={len(selected)} pending={len(pending_answers)}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.answer_workers) as pool:
        futures = {
            pool.submit(
                _answer_row,
                row,
                details["results"][row["id"]],
                settings,
                args,
            ): row
            for row in pending_answers
        }
        completed = len(selected) - len(pending_answers)
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = details["results"][row["id"]]
                result["answer_error"] = (
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
            with write_lock:
                details["results"][row["id"]] = result
                completed += 1
                _atomic_json(details_path, details)
                print(
                    f"answers {completed}/{len(selected)} id={row['id']}",
                    flush=True,
                )

    incomplete = [
        row["id"] for row in selected
        if "systems" not in details["results"][row["id"]]
        or details["results"][row["id"]].get("answer_error")
    ]
    if incomplete:
        raise SystemExit(
            f"Incomplete rows remain ({len(incomplete)}): {incomplete[:10]}"
        )
    finished = [
        details["results"][row["id"]] for row in selected
    ]
    details["summary"] = summarize(finished, args.seed)
    details["generated_at_utc"] = datetime.now(
        timezone.utc
    ).isoformat()
    _atomic_json(details_path, details)
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(details),
        encoding="utf-8",
    )
    print(json.dumps(details["summary"], ensure_ascii=False, indent=2))
    print(f"details: {details_path}")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
