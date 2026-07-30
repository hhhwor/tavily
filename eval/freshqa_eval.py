"""FreshQA evaluation for the local chukonu-web-search service.

The runner compares a fixed answer model with and without retrieved evidence.
It uses the latest pinned FreshQA spreadsheet snapshot, checkpoints every item,
and reports strict/relaxed FreshEval-style judgments plus deterministic answer
containment as a secondary metric.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from eval.freshqa_reporting import render_report, summarize
from src.config import Settings


SNAPSHOT_DATE = "2026-04-21"
SNAPSHOT_SHEET_ID = "1_8mi-yuK30mvoDJu1KQXD6ODem7MKMcIgVAwDSzJkjM"
SNAPSHOT_URL = (
    "https://docs.google.com/spreadsheets/d/"
    f"{SNAPSHOT_SHEET_ID}/export?format=csv"
)
OFFICIAL_REPO_COMMIT = "7d2d3683991916f3633e480548a6aa5c9a62e3db"

ANSWER_SYSTEM = """You are a concise question-answering assistant.
Answer in English, directly and definitively, using at most two sentences. If
the question has a false premise, explicitly correct it. Do not add citations,
source descriptions, lists, or tangential facts. If evidence is provided, use
only that evidence; if it is insufficient, say so. The current date is
{evaluation_date} UTC."""

JUDGE_SYSTEM = """You evaluate answers against FreshQA reference answers.
Return JSON only with keys strict, relaxed, and reason.

strict=true only when the response gives a confident, definitive correct
answer, explicitly corrects any false premise, contains no contradictory or
hallucinated extra claim, and is well formed.

relaxed=true when the primary answer is correct or obviously inferable and no
extra information contradicts or materially reshapes it. Minor irrelevant,
outdated, hallucinated, or ill-formed details may be ignored. A false-premise
question still requires the response to point out the false premise.

Numerical approximations are incorrect unless a reference answer permits them.
Judge only against the supplied reference answers; do not substitute outside
knowledge. Responses in any language are acceptable. The false_premise field is
authoritative: require premise correction only when it is true. Do not penalize
correct extra details unless they are inaccurate or contradictory. Treat the
supplied evaluation_date as the current date. If uncertain, use false."""


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _normalized(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold(), flags=re.UNICODE)


def _contains_reference(response: str, answers: Iterable[str]) -> bool:
    normalized_response = _normalized(response)
    return any(
        normalized_answer and normalized_answer in normalized_response
        for normalized_answer in (_normalized(item) for item in answers)
    )


def _download_dataset(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    response = requests.get(SNAPSHOT_URL, timeout=45)
    response.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return response.content


def _load_rows(path: Path, split: str) -> tuple[list[dict[str, str]], str]:
    raw = _download_dataset(path)
    table = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    header_index = next(index for index, row in enumerate(table) if row and row[0] == "id")
    header = table[header_index]
    rows = [
        dict(zip(header, row))
        for row in table[header_index + 1 :]
        if row and len(row) == len(header) and row[1] == split
    ]
    return rows, hashlib.sha256(raw).hexdigest()


def _answers(row: dict[str, str]) -> list[str]:
    return [row[f"answer_{index}"] for index in range(10) if row[f"answer_{index}"]]


class ChatClient:
    def __init__(self, settings: Settings, model: str, timeout: float):
        self.url = settings.siliconflow_base_url.rstrip("/") + "/chat/completions"
        self.key = settings.siliconflow_api_key
        self.model = model
        self.timeout = timeout

    def call(
        self, system: str, user: str, *, max_tokens: int, json_output: bool = False
    ) -> tuple[str, dict[str, int]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        last_error = "unknown error"
        for attempt in range(3):
            try:
                response = requests.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = f"HTTP {response.status_code}"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                response.raise_for_status()
                body = response.json()
                text = body["choices"][0]["message"]["content"].strip()
                usage = body.get("usage") or {}
                return text, {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                }
            except (requests.RequestException, KeyError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{self.model} failed after retries: {last_error}")


class SearchClient:
    def __init__(self, settings: Settings, url: str, timeout: float, limit: int):
        self.url = url
        self.token = next(iter(settings.auth_tokens), "")
        self.timeout = timeout
        self.limit = limit

    def search(self, query: str) -> dict[str, Any]:
        response = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"query": query, "limit": self.limit, "source_types": ["web"]},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def _evidence_prompt(response: dict[str, Any], max_chars: int) -> str:
    blocks: list[str] = []
    remaining = max_chars
    for index, item in enumerate(response.get("evidence", []), start=1):
        passage = (item.get("passage") or {}).get("text") or ""
        block = (
            f"[{index}] {item.get('title', '')}\n"
            f"Source: {item.get('source', '')}; URL: {item.get('url', '')}\n"
            f"{passage}\n"
        )
        block = block[:remaining]
        if block:
            blocks.append(block)
            remaining -= len(block)
        if remaining <= 0:
            break
    return "\n".join(blocks)


def _parse_judgment(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("judge did not return JSON")
    value = json.loads(match.group())
    return {
        "strict": bool(value.get("strict")),
        "relaxed": bool(value.get("relaxed")),
        "reason": str(value.get("reason") or "")[:800],
    }


def _judge(
    client: ChatClient,
    row: dict[str, str],
    response: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    payload = {
        "question": row["question"],
        "correct_answers": _answers(row),
        "false_premise": row["false_premise"],
        "evaluation_date": datetime.now(timezone.utc).date().isoformat(),
        "model_response": response,
    }
    text, usage = client.call(
        JUDGE_SYSTEM,
        json.dumps(payload, ensure_ascii=False),
        max_tokens=320,
        json_output=True,
    )
    return _parse_judgment(text), usage


def _evaluate_one(
    row: dict[str, str],
    answer_client: ChatClient,
    judge_client: ChatClient,
    search_client: SearchClient,
    evaluation_date: str,
    evidence_chars: int,
    cached: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row["id"],
        "question": row["question"],
        "metadata": {key: row[key] for key in (
            "effective_year", "next_review", "false_premise", "num_hops", "fact_type"
        )},
        "answers": _answers(row),
    }
    system = ANSWER_SYSTEM.format(evaluation_date=evaluation_date)

    started = time.perf_counter()
    baseline_answer, baseline_usage = answer_client.call(
        system, row["question"], max_tokens=128
    )
    result["baseline"] = {
        "answer": baseline_answer,
        "answer_ms": round((time.perf_counter() - started) * 1000, 1),
        "usage": baseline_usage,
        "contains_reference": _contains_reference(baseline_answer, _answers(row)),
    }

    if cached and "engine" in cached:
        prior = cached["engine"]
        search_response = {
            "status": prior.get("status"),
            "evidence": [
                {
                    "source": item.get("source"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "passage": {"text": item.get("passage", "")},
                }
                for item in prior.get("evidence", [])
            ],
            "failures": prior.get("failures", []),
            "retrieval_assessment": prior.get("retrieval_assessment", {}),
        }
        search_ms = prior.get("search_ms", 0)
    else:
        started = time.perf_counter()
        search_response = search_client.search(row["question"])
        search_ms = round((time.perf_counter() - started) * 1000, 1)
    evidence = _evidence_prompt(search_response, evidence_chars)
    started = time.perf_counter()
    engine_answer, engine_usage = answer_client.call(
        system,
        f"Question: {row['question']}\n\nRetrieved evidence:\n{evidence}",
        max_tokens=128,
    )
    result["engine"] = {
        "answer": engine_answer,
        "search_ms": search_ms,
        "answer_ms": round((time.perf_counter() - started) * 1000, 1),
        "usage": engine_usage,
        "contains_reference": _contains_reference(engine_answer, _answers(row)),
        "status": search_response.get("status"),
        "evidence_count": len(search_response.get("evidence", [])),
        "sources": sorted({item.get("source", "") for item in search_response.get("evidence", [])}),
        "failures": search_response.get("failures", []),
        "retrieval_assessment": search_response.get("retrieval_assessment", {}),
        "evidence": [
            {
                "source": item.get("source"),
                "title": item.get("title"),
                "url": item.get("url"),
                "passage": ((item.get("passage") or {}).get("text") or "")[:2000],
            }
            for item in search_response.get("evidence", [])
        ],
    }

    for name in ("baseline", "engine"):
        started = time.perf_counter()
        judgment, usage = _judge(judge_client, row, result[name]["answer"])
        result[name]["judgment"] = judgment
        result[name]["judge_usage"] = usage
        result[name]["judge_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-cache", default=f"eval/cache/freshqa_{SNAPSHOT_DATE}.csv")
    parser.add_argument("--split", choices=("TEST", "DEV"), default="TEST")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--search-url", default="http://127.0.0.1:8000/search")
    parser.add_argument("--search-limit", type=int, default=8)
    parser.add_argument("--search-timeout", type=float, default=45)
    parser.add_argument("--model-timeout", type=float, default=90)
    parser.add_argument("--evidence-chars", type=int, default=12_000)
    parser.add_argument("--answer-model", default="Qwen/Qwen3-30B-A3B-Instruct-2507")
    parser.add_argument("--judge-model", default="deepseek-ai/DeepSeek-V3.2")
    parser.add_argument("--evaluation-date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--details-path", default="eval/freshqa_chukonu_details.json")
    parser.add_argument("--report-path", default="eval/freshqa_chukonu_report.md")
    parser.add_argument(
        "--refresh-answers",
        action="store_true",
        help="Regenerate answers/judgments while reusing saved search evidence.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.siliconflow_api_key:
        raise SystemExit("SILICONFLOW_API_KEY is required")
    if not settings.auth_tokens:
        raise SystemExit("API_AUTH_TOKEN is required")

    all_rows, dataset_hash = _load_rows(Path(args.dataset_cache), args.split)
    rng = random.Random(args.seed)
    selected = (
        rng.sample(all_rows, args.sample_size)
        if 0 < args.sample_size < len(all_rows)
        else list(all_rows)
    )
    selected.sort(key=lambda row: int(row["id"]))
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
        "official_repo_commit": OFFICIAL_REPO_COMMIT,
    }
    fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).hexdigest()
    details_path = Path(args.details_path)
    details: dict[str, Any] = {
        "config": config,
        "config_fingerprint": fingerprint,
        "dataset_url": SNAPSHOT_URL,
        "dataset_sha256": dataset_hash,
        "results": {},
    }
    if details_path.exists():
        cached = json.loads(details_path.read_text(encoding="utf-8"))
        if cached.get("config_fingerprint") != fingerprint:
            raise SystemExit("Existing details use a different config; choose another output path")
        details = cached

    answer_client = ChatClient(settings, args.answer_model, args.model_timeout)
    judge_client = ChatClient(settings, args.judge_model, args.model_timeout)
    search_client = SearchClient(
        settings, args.search_url, args.search_timeout, args.search_limit
    )
    pending = list(selected) if args.refresh_answers else [
        row
        for row in selected
        if row["id"] not in details["results"]
        or "error" in details["results"][row["id"]]
    ]
    write_lock = threading.Lock()
    completed = len(selected) - len(pending)
    print(f"FreshQA selected={len(selected)} resumed={completed} pending={len(pending)}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _evaluate_one,
                row,
                answer_client,
                judge_client,
                search_client,
                args.evaluation_date,
                args.evidence_chars,
                details["results"].get(row["id"]) if args.refresh_answers else None,
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
                    "metadata": {key: row[key] for key in (
                        "effective_year", "next_review", "false_premise", "num_hops", "fact_type"
                    )},
                    "answers": _answers(row),
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                }
            with write_lock:
                details["results"][row["id"]] = result
                completed += 1
                details["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
                _atomic_json(details_path, details)
            state = "ok" if "error" not in result else "ERROR"
            print(
                f"[{completed:03}/{len(selected):03}] id={row['id']} {state} "
                f"{row['question'][:58]}",
                flush=True,
            )

    errors = [row for row in details["results"].values() if "error" in row]
    if errors:
        raise SystemExit(f"{len(errors)} items failed; rerun to resume after fixing errors")
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
