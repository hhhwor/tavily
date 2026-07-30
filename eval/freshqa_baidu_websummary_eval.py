"""Paired FreshQA comparison: Chukonu vs Baidu ``/web_summary``.

One Baidu call feeds two tracks:
1. references -> the same fixed answer model used for Chukonu;
2. Baidu's native answer for end-to-end product evaluation.
"""
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

from eval.baidu_highperf_ab import _highperf_search
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
from eval.freshqa_baidu_websummary_reporting import (
    SYSTEMS,
    render_report,
    summarize,
)
from src.config import Settings


BAIDU_URL = "https://qianfan.baidubce.com/v2/ai_search/web_summary"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _freshqa_instruction(evaluation_date: str) -> str:
    return (
        "You are a concise question-answering assistant. Answer in English, "
        "directly and definitively, using at most two sentences. If the question "
        "has a false premise, explicitly correct it. Do not add source descriptions, "
        f"lists, or tangential facts. The current date is {evaluation_date} UTC."
    )


def _metadata(row: dict[str, str]) -> dict[str, str]:
    keys = ("effective_year", "next_review", "false_premise", "num_hops", "fact_type")
    return {key: row[key] for key in keys}


def _selected_rows(
    rows: list[dict[str, str]], sample_size: int, seed: int, max_items: int
) -> list[dict[str, str]]:
    rng = random.Random(seed)
    selected = (
        rng.sample(rows, sample_size)
        if 0 < sample_size < len(rows)
        else list(rows)
    )
    selected.sort(key=lambda row: int(row["id"]))
    return selected[:max_items] if max_items > 0 else selected


def _validated_baseline(
    path: Path,
    selected: list[dict[str, str]],
    dataset_hash: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "split": args.split,
        "seed": args.seed,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "evaluation_date": args.evaluation_date,
    }
    mismatch = {
        key: (saved.get("config", {}).get(key), value)
        for key, value in expected.items()
        if saved.get("config", {}).get(key) != value
    }
    if saved.get("dataset_sha256") != dataset_hash:
        mismatch["dataset_sha256"] = (saved.get("dataset_sha256"), dataset_hash)
    for row in selected:
        prior = saved.get("results", {}).get(row["id"])
        if not prior or prior.get("question") != row["question"] or "baseline" not in prior:
            mismatch[f"row:{row['id']}"] = ("missing/different", "expected")
    if mismatch:
        raise ValueError(f"Baseline is not comparable: {mismatch}")
    return saved


def _retry(call: Callable[[], Any], valid: Callable[[Any], bool]) -> tuple[Any, int]:
    last: Any = None
    for attempt in range(3):
        try:
            last = call()
            if valid(last):
                return last, attempt
        except requests.RequestException as exc:
            last = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return last, 2


def _compact_evidence(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "source": item.get("source"),
            "title": item.get("title"),
            "url": item.get("url"),
            "passage": ((item.get("passage") or {}).get("text") or "")[:2000],
        }
        for item in response.get("evidence", [])
    ]


def _baidu_response(raw: dict[str, Any], limit: int) -> dict[str, Any]:
    evidence = []
    for item in (raw.get("references") or [])[:limit]:
        evidence.append(
            {
                "source": "baidu-web-summary",
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "published_date": item.get("date"),
                "passage": {
                    "text": str(item.get("content") or item.get("snippet") or "")
                },
                "metadata": {
                    "website": item.get("website") or item.get("web_anchor"),
                    "rerank_score": item.get("rerank_score"),
                    "authority_score": item.get("authority_score"),
                },
            }
        )
    return {
        "status": "complete" if raw.get("ok") and evidence else "failed",
        "evidence": evidence,
        "failures": [] if raw.get("ok") else [raw.get("error") or {"message": "failed"}],
        "retrieval_assessment": {
            "status": "usable" if evidence else "insufficient",
            "gaps": [] if evidence else ["NO_EVIDENCE"],
        },
    }


def _timed_search(client: SearchClient, query: str) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.search(query)
    return {
        "raw": response,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _retrieve_pair(
    row: dict[str, str],
    settings: Settings,
    args: argparse.Namespace,
) -> dict[str, Any]:
    search_client = SearchClient(
        settings, args.search_url, args.search_timeout, args.search_limit
    )

    def chukonu() -> dict[str, Any]:
        return _timed_search(search_client, row["question"])

    def baidu() -> dict[str, Any]:
        with requests.Session() as session:
            return _highperf_search(
                session,
                settings.qianfan_api_key,
                row["question"],
                args.search_limit,
                full_content=True,
                stream=True,
                instruction=_freshqa_instruction(args.evaluation_date),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        ch_future = pool.submit(
            _retry,
            chukonu,
            lambda value: value.get("raw", {}).get("status")
            in {"ok", "complete", "partial"},
        )
        ba_future = pool.submit(
            _retry,
            baidu,
            lambda value: bool(value.get("ok") and value.get("references")),
        )
        ch_value, ch_retries = ch_future.result()
        ba_value, ba_retries = ba_future.result()

    ch_raw = ch_value.get("raw") or {}
    ba_raw = ba_value if isinstance(ba_value, dict) else {}
    ba_normalized = _baidu_response(ba_raw, args.search_limit)
    return {
        "chukonu": {
            "status": ch_raw.get("status", "failed"),
            "search_ms": ch_value.get("elapsed_ms", 0),
            "retries": ch_retries,
            "evidence_count": len(ch_raw.get("evidence", [])),
            "sources": sorted(
                {item.get("source", "") for item in ch_raw.get("evidence", [])}
            ),
            "failures": ch_raw.get("failures", []),
            "prompt": _evidence_prompt(ch_raw, args.evidence_chars),
            "evidence": _compact_evidence(ch_raw),
        },
        "baidu": {
            "status": ba_normalized["status"],
            "request_id": ba_raw.get("request_id"),
            "retries": ba_retries,
            "first_reference_ms": ba_raw.get("first_reference_ms"),
            "first_token_ms": ba_raw.get("first_token_ms"),
            "total_ms": ba_raw.get("elapsed_ms", 0),
            "evidence_count": len(ba_normalized["evidence"]),
            "native_answer": str(ba_raw.get("answer") or ""),
            "native_answer_empty": not bool(ba_raw.get("answer")),
            "failures": ba_normalized["failures"],
            "prompt": _evidence_prompt(ba_normalized, args.evidence_chars),
            "evidence": _compact_evidence(ba_normalized),
        },
    }


def _judge_at_date(
    client: ChatClient,
    row: dict[str, str],
    answer: str,
    evaluation_date: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    if not answer.strip():
        return (
            {"strict": False, "relaxed": False, "reason": "Empty answer"},
            {"prompt_tokens": 0, "completion_tokens": 0},
        )
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
    system = ANSWER_SYSTEM.format(evaluation_date=args.evaluation_date)

    def fixed(name: str) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        answer, usage = answer_client.call(
            system,
            f"Question: {row['question']}\n\nRetrieved evidence:\n"
            f"{result['retrieval'][name]['prompt']}",
            max_tokens=128,
        )
        return answer, {
            "answer_ms": round((time.perf_counter() - started) * 1000, 1),
            "usage": usage,
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        ch_future = pool.submit(fixed, "chukonu")
        ba_future = pool.submit(fixed, "baidu")
        ch_answer, ch_meta = ch_future.result()
        ba_answer, ba_meta = ba_future.result()

    result["systems"] = {
        "chukonu_fixed": {"answer": ch_answer, **ch_meta},
        "baidu_fixed": {"answer": ba_answer, **ba_meta},
        "baidu_native": {
            "answer": result["retrieval"]["baidu"]["native_answer"],
            "answer_ms": result["retrieval"]["baidu"]["total_ms"],
            "usage": {},
        },
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            name: pool.submit(
                _judge_at_date,
                judge_client,
                row,
                result["systems"][name]["answer"],
                args.evaluation_date,
            )
            for name in SYSTEMS
        }
        for name, future in futures.items():
            judgment, usage = future.result()
            item = result["systems"][name]
            item["judgment"] = judgment
            item["judge_usage"] = usage
            item["contains_reference"] = _contains_reference(
                item["answer"], _answers(row)
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-cache", default=f"eval/cache/freshqa_{SNAPSHOT_DATE}.csv")
    parser.add_argument("--split", choices=("TEST", "DEV"), default="TEST")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--search-url", default="http://127.0.0.1:8000/search")
    parser.add_argument("--search-limit", type=int, default=8)
    parser.add_argument("--search-timeout", type=float, default=60)
    parser.add_argument("--model-timeout", type=float, default=90)
    parser.add_argument("--evidence-chars", type=int, default=12_000)
    parser.add_argument("--baidu-unit-cost-rmb", type=float, default=0.060)
    parser.add_argument("--answer-model", default="Qwen/Qwen3-30B-A3B-Instruct-2507")
    parser.add_argument("--judge-model", default="deepseek-ai/DeepSeek-V3.2")
    parser.add_argument("--evaluation-date", default="2026-07-29")
    parser.add_argument("--baseline-details", default="eval/freshqa_multisource_details.json")
    parser.add_argument("--details-path", default="eval/freshqa_baidu_websummary_details.json")
    parser.add_argument("--report-path", default="eval/freshqa_baidu_websummary_report.md")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.qianfan_api_key:
        raise SystemExit("QIANFAN_API_KEY is required")
    if not settings.siliconflow_api_key:
        raise SystemExit("SILICONFLOW_API_KEY is required")
    if not settings.auth_tokens:
        raise SystemExit("API_AUTH_TOKEN is required")
    all_rows, dataset_hash = _load_rows(Path(args.dataset_cache), args.split)
    selected = _selected_rows(all_rows, args.sample_size, args.seed, args.max_items)
    baseline_path = Path(args.baseline_details)
    baseline = _validated_baseline(
        baseline_path, selected, dataset_hash, args
    )
    config = {
        "split": args.split,
        "sample_size": len(selected),
        "source_sample_size": args.sample_size,
        "seed": args.seed,
        "search_url": args.search_url,
        "baidu_url": BAIDU_URL,
        "search_limit": args.search_limit,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "evaluation_date": args.evaluation_date,
        "evidence_chars": args.evidence_chars,
        "baidu_unit_cost_rmb": args.baidu_unit_cost_rmb,
        "official_repo_commit": OFFICIAL_REPO_COMMIT,
        "baseline_sha256": _sha256(baseline_path),
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
                "baseline": copy.deepcopy(
                    baseline["results"][row["id"]]["baseline"]
                ),
            },
        )
    _atomic_json(details_path, details)

    pending = [
        row
        for row in selected
        if "retrieval" not in details["results"][row["id"]]
        or details["results"][row["id"]]["retrieval"].get("fatal_error")
    ]
    print(f"retrieval selected={len(selected)} pending={len(pending)}", flush=True)
    write_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_retrieve_pair, row, settings, args): row for row in pending
        }
        completed = len(selected) - len(pending)
        for future in as_completed(futures):
            row = futures[future]
            try:
                retrieval = future.result()
            except Exception as exc:
                retrieval = {
                    "fatal_error": f"{type(exc).__name__}: {str(exc)[:500]}"
                }
            with write_lock:
                details["results"][row["id"]]["retrieval"] = retrieval
                completed += 1
                _atomic_json(details_path, details)
            print(f"[R {completed:03}/{len(selected):03}] id={row['id']}", flush=True)
    fatal = [
        row for row in selected
        if details["results"][row["id"]]["retrieval"].get("fatal_error")
    ]
    if fatal:
        raise SystemExit(f"{len(fatal)} retrieval rows failed; rerun to resume")

    pending = [
        row for row in selected if "systems" not in details["results"][row["id"]]
    ]
    print(f"answers selected={len(selected)} pending={len(pending)}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _answer_row,
                row,
                details["results"][row["id"]],
                settings,
                args,
            ): row
            for row in pending
        }
        completed = len(selected) - len(pending)
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = details["results"][row["id"]]
                result["answer_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            with write_lock:
                details["results"][row["id"]] = result
                completed += 1
                _atomic_json(details_path, details)
            state = "ok" if "answer_error" not in result else "ERROR"
            print(
                f"[A {completed:03}/{len(selected):03}] id={row['id']} {state}",
                flush=True,
            )
    errors = [
        details["results"][row["id"]]
        for row in selected
        if "systems" not in details["results"][row["id"]]
    ]
    if errors:
        raise SystemExit(f"{len(errors)} answer rows failed; rerun to resume")
    ordered = [details["results"][row["id"]] for row in selected]
    details["summary"] = summarize(ordered, args.seed)
    details["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(details_path, details)
    Path(args.report_path).write_text(render_report(details), encoding="utf-8")
    print(f"report={args.report_path} details={args.details_path}", flush=True)


if __name__ == "__main__":
    main()
