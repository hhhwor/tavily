"""End-to-end evaluation for the agent search API.

Unlike run_eval.py, this runner does not assemble providers/configs by hand.
It calls the real SearchEngine.search() path, or an HTTP /search endpoint when
--endpoint is provided, then scores the whole returned bundle.

Usage:
  .venv311/bin/python -m eval.run_e2e_eval --max-queries 6 --no-judge
  .venv311/bin/python -m eval.run_e2e_eval --endpoint http://127.0.0.1:8000/search
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from eval.e2e_judge import E2EBundleJudge, quality_value
from src.engine import SearchEngine
from src.models import SearchResponse

_CACHE_DIR = "eval/cache"
_REPORT_PATH = "eval/e2e_report.md"

_TIMELY_HINTS = re.compile(
    r"今天|今日|本周|这周|本月|今年|最近|最新|近期|实时|"
    r"\btoday\b|\bthis week\b|\bthis month\b|\bthis year\b|"
    r"\blatest\b|\brecent\b|\bnewest\b",
    re.I,
)
_YEAR = re.compile(r"\b20\d{2}\b")


def load_queries(path: str, limit: int) -> List[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


def _bool_arg(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = value.lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError("expected true/false")


def _expected(row: dict) -> Dict[str, bool]:
    q = row["query"]
    qtype = row.get("type", "")
    academic = bool(row.get("expected_academic", qtype == "academic"))
    patent = bool(row.get("expected_patent", qtype == "patent"))
    if "expected_time_sensitive" in row:
        timely = bool(row["expected_time_sensitive"])
    else:
        timely = qtype == "timely" or bool(_TIMELY_HINTS.search(q)) or bool(_YEAR.search(q))
    web = bool(row.get("expected_web", not academic and not patent))
    return {
        "web": web,
        "academic": academic,
        "patent": patent,
        "time_sensitive": timely,
    }


def _search_payload(row: dict, args: argparse.Namespace) -> dict:
    payload: Dict[str, Any] = {"query": row["query"], "top_k": args.k}
    for key in (
        "include_academic",
        "include_patent",
        "rerank_enabled",
        "rerank_backend",
        "rerank_model",
        "rerank_threshold",
        "fusion_enabled",
        "rewrite_enabled",
    ):
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    return payload


def _cache_key(payload: dict, endpoint: str) -> str:
    raw = json.dumps({"endpoint": endpoint or "inprocess", "payload": payload}, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load_response_cache(path: str) -> Dict[str, dict]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_response_cache(path: str, cache: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _api_token(args: argparse.Namespace) -> str:
    if args.api_token:
        return args.api_token
    env_token = os.getenv("EVAL_API_TOKEN") or os.getenv("API_AUTH_TOKEN", "")
    return env_token.split(",", 1)[0].strip()


def call_search(
    row: dict,
    args: argparse.Namespace,
    engine: Optional[SearchEngine],
    response_cache: Dict[str, dict],
) -> Tuple[SearchResponse, int, bool]:
    payload = _search_payload(row, args)
    ck = _cache_key(payload, args.endpoint)
    if args.cache_responses and ck in response_cache:
        return SearchResponse(**response_cache[ck]["response"]), response_cache[ck]["elapsed_ms"], True

    t0 = time.time()
    if args.endpoint:
        import requests

        headers = {"Content-Type": "application/json"}
        token = _api_token(args)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.post(args.endpoint, json=payload, headers=headers, timeout=args.timeout)
        resp.raise_for_status()
        response = SearchResponse(**resp.json())
    else:
        assert engine is not None
        response = engine.search(**_engine_kwargs(payload))
    elapsed_ms = int((time.time() - t0) * 1000)

    if args.cache_responses:
        response_cache[ck] = {"elapsed_ms": elapsed_ms, "response": response.model_dump()}
    return response, elapsed_ms, False


def _engine_kwargs(payload: dict) -> dict:
    return {
        "query": payload["query"],
        "top_k": payload["top_k"],
        "include_academic": payload.get("include_academic"),
        "include_patent": payload.get("include_patent"),
        "rerank_enabled": payload.get("rerank_enabled"),
        "rerank_backend": payload.get("rerank_backend"),
        "rerank_model": payload.get("rerank_model"),
        "rerank_threshold": payload.get("rerank_threshold"),
        "fusion_enabled": payload.get("fusion_enabled"),
        "rewrite_enabled": payload.get("rewrite_enabled"),
    }


def route_metrics(row: dict, resp: SearchResponse) -> dict:
    exp = _expected(row)
    checks = {
        "academic_route": (resp.academic_query is not None) == exp["academic"],
        "patent_route": (resp.patent_query is not None) == exp["patent"],
        "timely_route": bool(resp.time_sensitive) == exp["time_sensitive"],
    }
    required = []
    if exp["web"]:
        required.append(bool(resp.results))
    if exp["academic"]:
        required.append(bool(resp.academic_results))
    if exp["patent"]:
        required.append(bool(resp.patent_results))
    coverage = sum(required) / len(required) if required else 1.0
    return {
        **checks,
        "route_score": sum(1.0 for ok in checks.values() if ok) / len(checks),
        "required_block_score": coverage,
        "expected": exp,
    }

def _avg(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _p95(xs: List[int]) -> int:
    if not xs:
        return 0
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def _latency_score(p95_ms: int, budget_ms: int) -> float:
    if p95_ms <= 0:
        return 0.0
    return min(1.0, budget_ms / p95_ms)


def _md_escape(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def build_report(
    rows: List[dict],
    judgments: Dict[str, dict],
    args: argparse.Namespace,
) -> str:
    latencies = [r["elapsed_ms"] for r in rows]
    p95_ms = _p95(latencies)
    route = _avg([r["route"]["route_score"] for r in rows])
    required = _avg([r["route"]["required_block_score"] for r in rows])
    latency = _latency_score(p95_ms, args.latency_budget_ms)

    has_judge = bool(judgments)
    if has_judge:
        qvals = [quality_value(judgments[r["query"]]) for r in rows if r["query"] in judgments]
        quality = _avg(qvals)
        final = 100 * (0.60 * quality + 0.20 * route + 0.10 * required + 0.10 * latency)
    else:
        quality = 0.0
        final = None

    mode = args.endpoint or "inprocess SearchEngine.search()"
    lines = [
        f"# E2E 评测报告 (n={len(rows)}, k={args.k})",
        "",
        f"- mode: `{mode}`",
        f"- judge: {'off (--no-judge)' if not has_judge else args.judge_model}",
        f"- latency_budget_p95: {args.latency_budget_ms} ms",
        "",
        "## 总览",
        "| Final | BundleQuality | Route | RequiredBlocks | P95 latency | LatencyScore |",
        "|-------|---------------|-------|----------------|-------------|--------------|",
    ]
    final_text = "N/A" if final is None else f"{final:.1f}"
    quality_text = "N/A" if not has_judge else f"{quality:.3f}"
    lines.append(
        f"| {final_text} | {quality_text} | {route:.3f} | {required:.3f} | "
        f"{p95_ms} ms | {latency:.3f} |"
    )

    lines += [
        "",
        "## 路由明细",
        "| Metric | Score |",
        "|--------|-------|",
        f"| academic_route_acc | {_avg([1.0 if r['route']['academic_route'] else 0.0 for r in rows]):.3f} |",
        f"| patent_route_acc | {_avg([1.0 if r['route']['patent_route'] else 0.0 for r in rows]):.3f} |",
        f"| timely_route_acc | {_avg([1.0 if r['route']['timely_route'] else 0.0 for r in rows]):.3f} |",
        "",
        "## 失败样本",
        "| Query | Type | Route | Blocks | Judge | Reason |",
        "|-------|------|-------|--------|-------|--------|",
    ]
    failures = _failure_rows(rows, judgments)
    if not failures:
        lines.append("| - | - | - | - | - | - |")
    else:
        for r in failures[:20]:
            j = judgments.get(r["query"], {})
            route_text = (
                f"a={int(r['route']['academic_route'])},"
                f"p={int(r['route']['patent_route'])},"
                f"t={int(r['route']['timely_route'])}"
            )
            blocks = f"web={r['web_n']},acad={r['academic_n']},pat={r['patent_n']}"
            judge_text = "N/A" if not j else f"{j['score']}/4"
            reason = j.get("reason", "")
            lines.append(
                f"| {_md_escape(r['query'])} | {_md_escape(r['type'])} | {route_text} | "
                f"{blocks} | {judge_text} | {_md_escape(reason)} |"
            )

    lines += [
        "",
        "## 全量样本",
        "| Query | Type | ms | Web | Academic | Patent | Route | Judge |",
        "|-------|------|----|-----|----------|--------|-------|-------|",
    ]
    for r in rows:
        j = judgments.get(r["query"], {})
        judge_text = "N/A" if not j else f"{j['score']}/4"
        lines.append(
            f"| {_md_escape(r['query'])} | {_md_escape(r['type'])} | {r['elapsed_ms']} | "
            f"{r['web_n']} | {r['academic_n']} | {r['patent_n']} | "
            f"{r['route']['route_score']:.2f} | {judge_text} |"
        )
    return "\n".join(lines) + "\n"


def _failure_rows(rows: List[dict], judgments: Dict[str, dict]) -> List[dict]:
    out = []
    for r in rows:
        j = judgments.get(r["query"])
        judge_bad = j is not None and j["score"] <= 2
        if r["route"]["route_score"] < 1 or r["route"]["required_block_score"] < 1 or judge_bad:
            out.append(r)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="eval/dataset.jsonl")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max-queries", type=int, default=0)
    ap.add_argument("--endpoint", default="", help="HTTP /search endpoint; omit for in-process engine")
    ap.add_argument("--api-token", default="", help="Bearer token for --endpoint")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--cache-responses", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--latency-budget-ms", type=int, default=8000)
    ap.add_argument("--include-academic", type=_bool_arg, default=None)
    ap.add_argument("--include-patent", type=_bool_arg, default=None)
    ap.add_argument("--rerank-enabled", type=_bool_arg, default=None)
    ap.add_argument("--rerank-backend", default=None)
    ap.add_argument("--rerank-model", default=None)
    ap.add_argument("--rerank-threshold", type=float, default=None)
    ap.add_argument("--fusion-enabled", type=_bool_arg, default=None)
    ap.add_argument("--rewrite-enabled", type=_bool_arg, default=None)
    args = ap.parse_args()

    queries = load_queries(args.dataset, args.max_queries)
    engine = None if args.endpoint else SearchEngine()
    os.makedirs(_CACHE_DIR, exist_ok=True)
    response_cache_path = os.path.join(_CACHE_DIR, "e2e_search.json")
    response_cache = _load_response_cache(response_cache_path) if args.cache_responses else {}

    rows: List[dict] = []
    responses: Dict[str, SearchResponse] = {}
    for idx, row in enumerate(queries, 1):
        resp, elapsed_ms, cache_hit = call_search(row, args, engine, response_cache)
        responses[row["query"]] = resp
        route = route_metrics(row, resp)
        rows.append({
            "query": row["query"],
            "type": row.get("type", ""),
            "elapsed_ms": elapsed_ms,
            "cache_hit": cache_hit,
            "web_n": len(resp.results),
            "academic_n": len(resp.academic_results),
            "patent_n": len(resp.patent_results),
            "route": route,
        })
        print(
            f"[{idx}/{len(queries)}] {row['query'][:28]}... "
            f"{elapsed_ms}ms web={len(resp.results)} "
            f"acad={len(resp.academic_results)} pat={len(resp.patent_results)}"
            + (" cache" if cache_hit else "")
        )

    if args.cache_responses:
        _save_response_cache(response_cache_path, response_cache)

    judgments: Dict[str, dict] = {}
    if not args.no_judge:
        print("\n[E2E judge] scoring response bundles...")
        judge = E2EBundleJudge(model=args.judge_model)
        judgments = judge.score_batch([(q, responses[q]) for q in responses])

    report = build_report(rows, judgments, args)
    print("\n" + report)
    with open(_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"-> wrote {_REPORT_PATH}")


if __name__ == "__main__":
    main()
