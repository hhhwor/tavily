"""20-query live comparison of Baidu standard AI Search Generation.

This runner reuses the completed web_search/web_summary cache from
``baidu_highperf_ab.py`` and adds the standard
``/v2/ai_search/chat/completions`` endpoint.  New network work is checkpointed
after every call so a retry does not repeat completed Baidu requests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

import requests

from eval import metrics as M
from eval.baidu_highperf_ab import (
    _ANSWER_INSTRUCTION,
    _REL_RUBRIC,
    ClaudeClient,
    _append_choice_text,
    _ask_json,
    _baseline_answer,
    _content_stats,
    _dedup_refs,
    _evidence,
    _error_payload,
    _headers,
    _load_json,
    _load_queries,
    _mean_dict,
    _metrics_for,
    _paired_bootstrap,
    _percentile,
    _rank_refs,
    _read_project_env,
    _ref_key,
    _save_json,
    _score_union,
)
from src.config import Settings
from src.ranking.adapters.siliconflow import SiliconFlowReranker


_STANDARD_URL = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"
_CACHE_VERSION = 1
_MODEL_PRICES_RMB_PER_1K = {
    # Official public online-inference prices checked on 2026-07-28.
    "deepseek-v4-flash": {
        "input": 0.001,
        "cached_input": 0.0002,
        "output": 0.002,
    },
    "ernie-4.5-turbo-32k": {
        "input": 0.0008,
        "cached_input": 0.0002,
        "output": 0.0032,
    },
}

_TRIPLE_RUBRIC = """你是联网问答质量评审。比较同一个 query 的三个回答 A、B、C。
每个回答都附带它实际使用的证据。只根据给出的证据评估，不使用外部知识。

分别评估：correctness（事实被证据支持）、completeness（覆盖核心意图）、
grounding（引用存在且支持结论）、freshness（时效问题是否足够新）。
避免位置偏好，不因篇幅更长而自动给高分。

只输出合法 JSON，不要输出 reason 或其他文字：
{"A":{"score":0,"correctness":0,"completeness":0,"grounding":0,"freshness":0},
 "B":{"score":0,"correctness":0,"completeness":0,"grounding":0,"freshness":0},
 "C":{"score":0,"correctness":0,"completeness":0,"grounding":0,"freshness":0},
 "winner":"A|B|C|tie"}
总分范围0-10；四个分项范围0-2。"""


def _standard_body(query: str, model: str, top_k: int, *, stream: bool) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": query}],
        "model": model,
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": min(top_k, 20)}],
        "search_mode": "required",
        "enable_deep_search": False,
        "enable_reasoning": False,
        "enable_corner_markers": True,
        "max_refer_search_items": min(top_k, 20),
        "instruction": _ANSWER_INSTRUCTION,
        "temperature": 1e-6,
        "top_p": 1e-10,
        "response_format": "text",
        "stream": stream,
    }


def _standard_search(
    session: requests.Session,
    api_key: str,
    query: str,
    model: str,
    top_k: int,
    *,
    stream: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    response = session.post(
        _STANDARD_URL,
        headers=_headers(api_key),
        json=_standard_body(query, model, top_k, stream=stream),
        timeout=(10, 240),
        stream=stream,
    )
    headers_ms = round((time.perf_counter() - started) * 1000, 1)
    if response.status_code != 200:
        return {
            **_error_payload(response),
            "headers_ms": headers_ms,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "model": model,
            "stream": stream,
        }

    if not stream:
        data = response.json()
        choices = data.get("choices") or []
        answer_text = "".join(
            str((choice.get("message") or {}).get("content") or "")
            for choice in choices
        )
        return {
            "ok": isinstance(data.get("references"), list) and bool(answer_text),
            "status": 200,
            "headers_ms": headers_ms,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "first_event_ms": None,
            "first_reference_ms": None,
            "first_token_ms": None,
            "request_id": data.get("request_id") or data.get("requestId"),
            "references": data.get("references") or [],
            "answer": answer_text,
            "reasoning": "".join(
                str((choice.get("message") or {}).get("reasoning_content") or "")
                for choice in choices
            ),
            "usage": data.get("usage") or {},
            "model": model,
            "stream": stream,
            "error": None if "references" in data else data,
        }

    response.encoding = "utf-8"
    first_event_ms: float | None = None
    first_reference_ms: float | None = None
    first_token_ms: float | None = None
    request_id: str | None = None
    references: list[dict[str, Any]] = []
    answer: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] = {}
    processing_states: list[dict[str, Any]] = []
    embedded_errors: list[dict[str, Any]] = []
    finish_reasons: list[str] = []
    parse_errors: list[str] = []
    event_count = 0

    def consume(payload: str) -> None:
        nonlocal first_event_ms, first_reference_ms, first_token_ms
        nonlocal request_id, references, event_count, usage
        payload = payload.strip()
        if not payload or payload == "[DONE]":
            return
        event_count += 1
        now_ms = round((time.perf_counter() - started) * 1000, 1)
        if first_event_ms is None:
            first_event_ms = now_ms
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            if len(parse_errors) < 3:
                parse_errors.append(payload[:300])
            return
        request_id = request_id or event.get("request_id") or event.get("requestId")
        if event.get("references") and not references:
            references = event["references"]
            first_reference_ms = now_ms
        if event.get("usage"):
            usage = dict(event["usage"])
        if event.get("code") or event.get("message"):
            embedded_errors.append({
                "code": event.get("code"),
                "message": str(event.get("message") or "")[:500],
            })
        before = len(answer)
        _append_choice_text(event, answer, reasoning)
        if len(answer) > before and first_token_ms is None:
            first_token_ms = now_ms
        for choice in event.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reasons.append(str(choice["finish_reason"]))
            state = (choice.get("delta") or {}).get("processing_state")
            if state:
                processing_states.append(state)

    event_lines: list[str] = []
    for raw in response.iter_lines(decode_unicode=True):
        if raw == "":
            if event_lines:
                consume("\n".join(event_lines))
                event_lines.clear()
            continue
        line = raw
        if line.startswith("data:"):
            line = line[5:].lstrip()
        event_lines.append(line)
    if event_lines:
        consume("\n".join(event_lines))

    return {
        "ok": bool(references) and bool(answer),
        "status": 200,
        "headers_ms": headers_ms,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "first_event_ms": first_event_ms,
        "first_reference_ms": first_reference_ms,
        "first_token_ms": first_token_ms,
        "event_count": event_count,
        "request_id": request_id,
        "references": references,
        "answer": "".join(answer),
        "reasoning": "".join(reasoning),
        "usage": usage,
        "processing_states": processing_states,
        "embedded_errors": embedded_errors,
        "finish_reasons": finish_reasons,
        "parse_errors": parse_errors,
        "model": model,
        "stream": stream,
        "error": (
            None
            if references and answer
            else {"message": "SSE 未完整返回 references 与 answer"}
        ),
    }


def _judge_relevance_three(
    claude: ClaudeClient,
    query: str,
    pool: Sequence[dict[str, Any]],
) -> dict[str, int]:
    documents = []
    keys = []
    for index, ref in enumerate(pool):
        keys.append(_ref_key(ref, index))
        documents.append({
            "id": index,
            "title": str(ref.get("title") or "")[:180],
            "date": str(ref.get("date") or "")[:40],
            "url": str(ref.get("url") or "")[:500],
            "text": str(ref.get("snippet") or ref.get("content") or "")[:700],
        })
    data = _ask_json(
        claude,
        _REL_RUBRIC,
        {"query": query, "documents": documents},
        2000,
    )
    labels = {
        int(item["id"]): max(0, min(3, int(item.get("rel", 0))))
        for item in data.get("labels") or []
        if "id" in item
    }
    if set(labels) != set(range(len(documents))):
        missing = sorted(set(range(len(documents))) - set(labels))
        raise ValueError(f"三方相关性 judge 缺失文档 id: {missing}")
    return {key: labels[index] for index, key in enumerate(keys)}


def _score_fields(raw: dict[str, Any]) -> dict[str, int]:
    def value(name: str, hi: int) -> int:
        try:
            number = int(raw.get(name, 0))
        except Exception:
            number = 0
        return max(0, min(hi, number))

    return {
        "score": value("score", 10),
        "correctness": value("correctness", 2),
        "completeness": value("completeness", 2),
        "grounding": value("grounding", 2),
        "freshness": value("freshness", 2),
    }


def _judge_three_answers(
    claude: ClaudeClient,
    query: str,
    candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    names = sorted(
        candidates,
        key=lambda name: hashlib.sha1(f"{query}\0{name}".encode()).hexdigest(),
    )
    positions = dict(zip(("A", "B", "C"), names))
    payload: dict[str, Any] = {"query": query}
    for position, name in positions.items():
        item = candidates[name]
        payload[position] = {
            "answer": item["answer"],
            "evidence": _evidence(item["references"]),
        }
    raw = _ask_json(claude, _TRIPLE_RUBRIC, payload, 900)
    result = {
        name: _score_fields(raw.get(position) or {})
        for position, name in positions.items()
    }
    winner_position = str(raw.get("winner") or "tie")
    result["winner"] = positions.get(winner_position, "tie")
    result["positions"] = positions
    return result


def _analyze_query(
    base: dict[str, Any],
    standard: dict[str, Any],
    labels: dict[str, int],
    sf_scores: dict[str, float],
    metric_k: int,
) -> dict[str, Any]:
    current_refs = base["current"]["references"]
    high_refs = base["high_full"]["references"]
    standard_refs = standard["references"]
    pool = _dedup_refs((current_refs, high_refs, standard_refs))
    rankings = {
        "current_source": _rank_refs(current_refs, None),
        "current_sf": _rank_refs(current_refs, sf_scores),
        "high_baidu": _rank_refs(high_refs, None, baidu=True),
        "high_sf": _rank_refs(high_refs, sf_scores),
        "standard_source": _rank_refs(standard_refs, None),
        "standard_sf": _rank_refs(standard_refs, sf_scores),
    }
    union_metrics = {
        name: _metrics_for(refs, labels, pool, metric_k)
        for name, refs in rankings.items()
    }
    standard_pool_metrics = {
        name: _metrics_for(rankings[name], labels, standard_refs, metric_k)
        for name in ("standard_source", "standard_sf")
    }

    def jaccard(left: Sequence[dict[str, Any]], right: Sequence[dict[str, Any]]) -> float:
        a = {_ref_key(ref, i) for i, ref in enumerate(left)}
        b = {_ref_key(ref, i) for i, ref in enumerate(right)}
        return len(a & b) / len(a | b) if a or b else 0.0

    return {
        "metrics_triple_union_pool": union_metrics,
        "metrics_standard_pool": standard_pool_metrics,
        "url_jaccard_standard_current": jaccard(standard_refs, current_refs),
        "url_jaccard_standard_high": jaccard(standard_refs, high_refs),
    }


def _run_qps_stage(
    api_key: str,
    model: str,
    rate: float,
    count: int,
) -> dict[str, Any]:
    barrier = threading.Barrier(count)
    started = time.perf_counter()

    def work(index: int) -> dict[str, Any]:
        session = requests.Session()
        barrier.wait()
        target = started + index / rate
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        issued = time.perf_counter()
        result = _standard_search(
            session,
            api_key,
            f"北京天气 标准版QPS探针{rate:g}-{index}",
            model,
            1,
            stream=False,
        )
        session.close()
        return {
            "index": index,
            "issued_ms": round((issued - started) * 1000, 1),
            "status": result.get("status"),
            "ok": result.get("ok", False),
            "elapsed_ms": result.get("elapsed_ms"),
            "usage": result.get("usage") or {},
            "error": result.get("error"),
        }

    with ThreadPoolExecutor(max_workers=count) as executor:
        requests_out = list(executor.map(work, range(count)))
    statuses = {item.get("status") for item in requests_out}
    return {
        "target_qps": rate,
        "request_count": count,
        "wall_ms": round((time.perf_counter() - started) * 1000, 1),
        "successes": sum(bool(item["ok"]) for item in requests_out),
        "statuses": {
            str(status): sum(item.get("status") == status for item in requests_out)
            for status in sorted(statuses, key=str)
        },
        "requests": requests_out,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _summarize(
    base_details: dict[str, Any],
    details: dict[str, Any],
    rows: list[dict[str, Any]],
    metric_k: int,
) -> dict[str, Any]:
    joined = [(base_details["queries"][item["query"]], details["queries"][item["query"]])
              for item in rows]
    names = (
        "current_source", "current_sf", "high_baidu", "high_sf",
        "standard_source", "standard_sf",
    )
    metrics = {
        name: _mean_dict([new["analysis"]["metrics_triple_union_pool"][name]
                          for _, new in joined])
        for name in names
    }
    standard_pool = {
        name: _mean_dict([new["analysis"]["metrics_standard_pool"][name]
                          for _, new in joined])
        for name in ("standard_source", "standard_sf")
    }
    standard_rows = [new["standard"] for _, new in joined]
    standard_ref = [row["first_reference_ms"] for row in standard_rows
                    if row.get("first_reference_ms") is not None]
    standard_token = [row["first_token_ms"] for row in standard_rows
                      if row.get("first_token_ms") is not None]
    standard_total = [row["elapsed_ms"] for row in standard_rows if row.get("answer")]
    high_ref = [base["high_full"]["first_reference_ms"] for base, _ in joined]
    high_total = [base["high_full"]["elapsed_ms"] for base, _ in joined]

    dims = ("score", "correctness", "completeness", "grounding", "freshness")
    systems = ("current_sf", "highperf", "standard_sf")
    answer_quality: dict[str, Any] = {
        system: {
            dim: statistics.fmean(new["answer_judge"][system][dim] for _, new in joined)
            for dim in dims
        }
        for system in systems
    }
    wins = {system: 0 for system in (*systems, "tie")}
    for _, new in joined:
        wins[new["answer_judge"]["winner"]] += 1
    answer_quality["wins"] = wins

    native_joined = [(base, new) for base, new in joined if new.get("native_answer_judge")]
    native_quality: dict[str, Any] = {"query_count": len(native_joined)}
    if native_joined:
        native_systems = ("current_sf", "highperf", "standard_native")
        for system in native_systems:
            native_quality[system] = {
                dim: statistics.fmean(new["native_answer_judge"][system][dim]
                                      for _, new in native_joined)
                for dim in dims
            }
        native_wins = {system: 0 for system in (*native_systems, "tie")}
        for _, new in native_joined:
            native_wins[new["native_answer_judge"]["winner"]] += 1
        native_quality["wins"] = native_wins

    usage_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    successful_standard_rows = [row for row in standard_rows if row.get("answer")]
    usage = {
        key: {
            "total": sum(int(row.get("usage", {}).get(key) or 0) for row in standard_rows),
            "mean_successful": (
                statistics.fmean(int(row.get("usage", {}).get(key) or 0)
                                  for row in successful_standard_rows)
                if successful_standard_rows else 0.0
            ),
        }
        for key in usage_keys
    }
    cached_prompt_tokens = sum(
        int((row.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        for row in successful_standard_rows
    )
    model = details["config"]["model"]
    prices = _MODEL_PRICES_RMB_PER_1K.get(model)
    uncached_prompt_tokens = usage["prompt_tokens"]["total"] - cached_prompt_tokens
    estimated_model_cost_rmb = None
    if prices:
        estimated_model_cost_rmb = (
            uncached_prompt_tokens * prices["input"] / 1000
            + cached_prompt_tokens * prices["cached_input"] / 1000
            + usage["completion_tokens"]["total"] * prices["output"] / 1000
        )
    usage["cached_prompt_tokens"] = cached_prompt_tokens
    usage["estimated_model_cost_rmb"] = estimated_model_cost_rmb
    usage["price_rmb_per_1k"] = prices

    def diffs(left: str, right: str, metric: str) -> list[float]:
        return [
            new["analysis"]["metrics_triple_union_pool"][left][metric]
            - new["analysis"]["metrics_triple_union_pool"][right][metric]
            for _, new in joined
        ]

    checks = {
        "standard_sf_minus_current_sf_ndcg": _paired_bootstrap(
            diffs("standard_sf", "current_sf", "ndcg"), 20260730
        ),
        "standard_sf_minus_current_sf_recall": _paired_bootstrap(
            diffs("standard_sf", "current_sf", "recall"), 20260731
        ),
        "standard_sf_minus_high_sf_ndcg": _paired_bootstrap(
            diffs("standard_sf", "high_sf", "ndcg"), 20260801
        ),
        "standard_source_minus_standard_sf_ndcg": _paired_bootstrap(
            diffs("standard_source", "standard_sf", "ndcg"), 20260802
        ),
    }
    return {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "comparison_base_generated_at_utc": (
            base_details.get("summary", {}).get("generated_at_utc")
        ),
        "query_count": len(rows),
        "metric_k": metric_k,
        "model": details["config"]["model"],
        "metrics_triple_union_pool": metrics,
        "metrics_standard_pool": standard_pool,
        "statistical_checks": checks,
        "latency_ms": {
            "standard_first_reference_p50": _percentile(standard_ref, 0.5),
            "standard_first_reference_p95": _percentile(standard_ref, 0.95),
            "standard_first_token_p50": _percentile(standard_token, 0.5),
            "standard_first_token_p95": _percentile(standard_token, 0.95),
            "standard_total_p50": _percentile(standard_total, 0.5),
            "standard_total_p95": _percentile(standard_total, 0.95),
            "high_first_reference_p50": _percentile(high_ref, 0.5),
            "high_first_reference_p95": _percentile(high_ref, 0.95),
            "high_total_p50": _percentile(high_total, 0.5),
            "high_total_p95": _percentile(high_total, 0.95),
        },
        "url_overlap": {
            "standard_current_jaccard_mean": statistics.fmean(
                new["analysis"]["url_jaccard_standard_current"] for _, new in joined
            ),
            "standard_high_jaccard_mean": statistics.fmean(
                new["analysis"]["url_jaccard_standard_high"] for _, new in joined
            ),
        },
        "content": {
            "standard": _content_stats(standard_rows),
            "current": _content_stats([base["current"] for base, _ in joined]),
            "highperf": _content_stats([base["high_full"] for base, _ in joined]),
        },
        "usage": usage,
        "generation": {
            "successes": sum(bool(row.get("answer")) for row in standard_rows),
            "failures": sum(not bool(row.get("answer")) for row in standard_rows),
            "blocked_code": details.get("generation_block", {}).get("code"),
            "blocked_message": details.get("generation_block", {}).get("message"),
        },
        "answer_quality": answer_quality,
        "native_answer_quality": native_quality,
        "qps_probe": details.get("qps_probe") or [],
    }


def _report(summary: dict[str, Any], details: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    metrics = summary["metrics_triple_union_pool"]
    latency = summary["latency_ms"]
    answers = summary["answer_quality"]
    checks = summary["statistical_checks"]
    generation = summary["generation"]
    successful_count = generation["successes"]
    query_count = summary["query_count"]
    lines = [
        f"# 百度标准版 vs 当前方案 vs 高性能版（n={summary['query_count']}，k={summary['metric_k']}）",
        "",
        f"- generated_at_utc: `{summary['generated_at_utc']}`",
        f"- 当前方案/高性能版基准时间：`{summary.get('comparison_base_generated_at_utc')}`",
        f"- 标准版模型：`{summary['model']}`",
        "- 标准版配置：`baidu_search_v2`, `search_mode=required`, `enable_deep_search=false`, `stream=true`",
        "- 三方候选 URL 并集由同一 LLM judge 按 0–3 标注；三方答案位置按 Query 确定性盲化",
        "",
        "## 检索与排序（三方 URL 并集为 Recall 分母）",
        "",
        "| 配置 | nDCG@10 | Recall@10 | P@10 | MRR |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in (
        "current_source", "current_sf", "high_baidu", "high_sf",
        "standard_source", "standard_sf",
    ):
        row = metrics[name]
        lines.append(
            f"| {name} | {_fmt(row['ndcg'])} | {_fmt(row['recall'])} | "
            f"{_fmt(row['precision'])} | {_fmt(row['mrr'])} |"
        )
    lines += ["", "### 配对统计", ""]
    for label, key in (
        ("standard_sf − current_sf nDCG", "standard_sf_minus_current_sf_ndcg"),
        ("standard_sf − current_sf Recall", "standard_sf_minus_current_sf_recall"),
        ("standard_sf − high_sf nDCG", "standard_sf_minus_high_sf_ndcg"),
        ("standard_source − standard_sf nDCG", "standard_source_minus_standard_sf_ndcg"),
    ):
        row = checks[key]
        lines.append(
            f"- {label}: `{_fmt(row['mean_delta'])}`，bootstrap 95% CI "
            f"`[{_fmt(row['ci95'][0])}, {_fmt(row['ci95'][1])}]`，胜/平/负 `{row['wins_ties_losses']}`"
        )
    lines += [
        "",
        f"- 标准版/当前 URL Jaccard 均值：`{_fmt(summary['url_overlap']['standard_current_jaccard_mean'])}`",
        f"- 标准版/高性能 URL Jaccard 均值：`{_fmt(summary['url_overlap']['standard_high_jaccard_mean'])}`",
        "",
        "## 延迟",
        "",
        "| 指标 | P50 | P95 |",
        "|---|---:|---:|",
        f"| 标准版首引用 | {_fmt(latency['standard_first_reference_p50'], 1)} ms | {_fmt(latency['standard_first_reference_p95'], 1)} ms |",
        f"| 标准版首答案 Token（n={successful_count}） | {_fmt(latency['standard_first_token_p50'], 1)} ms | {_fmt(latency['standard_first_token_p95'], 1)} ms |",
        f"| 标准版流完成（n={successful_count}） | {_fmt(latency['standard_total_p50'], 1)} ms | {_fmt(latency['standard_total_p95'], 1)} ms |",
        f"| 高性能首引用 | {_fmt(latency['high_first_reference_p50'], 1)} ms | {_fmt(latency['high_first_reference_p95'], 1)} ms |",
        f"| 高性能流完成 | {_fmt(latency['high_total_p50'], 1)} ms | {_fmt(latency['high_total_p95'], 1)} ms |",
        "",
        "## 答案质量：统一生成器对照（三方同场盲评）",
        "",
        f"为完成全部 {query_count} 条来源质量对比，`standard_sf` 使用与 "
        "`current_sf` 相同的固定证据回答器；高性能版仍使用其原生答案。"
        "本节用于比较可部署管线，不是纯模型对比。",
        "",
        "| 配置 | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("current_sf", "highperf", "standard_sf"):
        row = answers[name]
        lines.append(
            f"| {name} | {_fmt(row['score'])} | {_fmt(row['correctness'])} | "
            f"{_fmt(row['completeness'])} | {_fmt(row['grounding'])} | {_fmt(row['freshness'])} |"
        )
    lines += [
        "",
        f"- 胜负：`{answers['wins']}`",
        "",
        (
            "### 标准版原生答案（完整样本）"
            if successful_count == query_count
            else "### 标准版原生答案（仅成功的子样本）"
        ),
        "",
    ]
    native = summary["native_answer_quality"]
    if native.get("query_count"):
        lines += [
            f"- 有效 Query：`{native['query_count']}/{query_count}`",
            "",
            "| 配置 | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name in ("current_sf", "highperf", "standard_native"):
            row = native[name]
            lines.append(
                f"| {name} | {_fmt(row['score'])} | {_fmt(row['correctness'])} | "
                f"{_fmt(row['completeness'])} | {_fmt(row['grounding'])} | {_fmt(row['freshness'])} |"
            )
        lines += ["", f"- 胜负：`{native['wins']}`", ""]
    lines += [
        f"- 标准版原生生成成功/失败：`{generation['successes']}/{generation['failures']}`",
        f"- 流内错误：`{generation.get('blocked_code')}` / `{generation.get('blocked_message')}`",
        "",
        "## 标准版内容与 Token",
        "",
        "| 接口 | 引用数 | 平均 content 字符 | 中位数 | P95 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ("current", "highperf", "standard"):
        row = summary["content"][name]
        lines.append(
            f"| {name} | {row['count']} | {_fmt(row['mean_content_chars'], 1)} | "
            f"{_fmt(row['median_content_chars'], 1)} | {_fmt(row['p95_content_chars'], 1)} |"
        )
    usage = summary["usage"]
    if usage["estimated_model_cost_rmb"] is not None:
        prices = usage["price_rmb_per_1k"]
        cost_line = (
            f"- 按 `{summary['model']}` 公开在线推理单价（输入/缓存输入/输出："
            f"`¥{prices['input']}/¥{prices['cached_input']}/¥{prices['output']}` 每千 Token）"
            f"估算 {successful_count} 条模型费用约 "
            f"`¥{_fmt(usage['estimated_model_cost_rmb'], 4)}`"
        )
    else:
        cost_line = f"- 未配置 `{summary['model']}` 的公开单价，未估算模型费用"
    lines += [
        "",
        f"- 标准版 prompt tokens：总计 `{usage['prompt_tokens']['total']}`，成功请求均值 `{_fmt(usage['prompt_tokens']['mean_successful'], 1)}`",
        f"- 标准版 completion tokens：总计 `{usage['completion_tokens']['total']}`，成功请求均值 `{_fmt(usage['completion_tokens']['mean_successful'], 1)}`",
        f"- 标准版 total tokens：总计 `{usage['total_tokens']['total']}`，成功请求均值 `{_fmt(usage['total_tokens']['mean_successful'], 1)}`",
        f"- 其中缓存命中 prompt tokens：`{usage['cached_prompt_tokens']}`",
        cost_line,
        "",
        "## 标准版 QPS 探针",
        "",
        "| 目标发送速率 | 请求数 | 成功 | HTTP 状态 | 墙钟耗时 |",
        "|---:|---:|---:|---|---:|",
    ]
    for stage in summary["qps_probe"]:
        lines.append(
            f"| {stage['target_qps']} QPS | {stage['request_count']} | {stage['successes']} | "
            f"`{stage['statuses']}` | {_fmt(stage['wall_ms'], 1)} ms |"
        )
    if not summary["qps_probe"]:
        reason = generation.get("blocked_code") or "未执行"
        lines.append(f"| 未执行 | 0 | 0 | `{reason}` | N/A |")
    lines += [
        "",
        "## 单 Query 明细",
        "",
        "| Query | 类型 | 标准引用 | 首引用ms | 总ms | standard_sf nDCG | 答案胜者 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for item in rows:
        query = item["query"]
        row = details["queries"][query]
        escaped = query.replace("|", "\\|")
        metric = row["analysis"]["metrics_triple_union_pool"]["standard_sf"]["ndcg"]
        lines.append(
            f"| {escaped} | {item.get('type', '')} | {len(row['standard']['references'])} | "
            f"{_fmt(row['standard'].get('first_reference_ms'), 1)} | "
            f"{_fmt(row['standard'].get('elapsed_ms'), 1)} | {_fmt(metric)} | "
            f"{row['answer_judge']['winner']} |"
        )
    lines += [
        "",
        "## 限制",
        "",
        f"- 当前方案/高性能版沿用 `{summary.get('comparison_base_generated_at_utc')}` "
        f"的缓存，标准版复测完成于 `{summary['generated_at_utc']}`；"
        "搜索结果具有时变性，因此跨源检索指标不是严格同时点 A/B。",
        f"- 结果只代表 `{summary['model']}`、关闭深搜索的标准版配置；更换模型会改变答案、延迟与费用。",
        "- relevance 与答案评分由单一 LLM judge 完成，尚未人工复核。",
        "- QPS 是四请求短突发，不代表长期吞吐或 SLA。",
        "- 标准版搜索免费额度不覆盖额外的大模型 Token 费用。",
        "",
    ]
    if generation["failures"]:
        lines.insert(
            -5,
            f"- 原生生成失败 {generation['failures']}/{query_count} 条；"
            f"流内错误为 `{generation.get('blocked_code')}` / "
            f"`{generation.get('blocked_message')}`。",
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="eval/dataset.jsonl")
    parser.add_argument("--base-details", default="eval/baidu_highperf_ab_details.json")
    parser.add_argument("--details", default="eval/baidu_standard_ab_details.json")
    parser.add_argument("--report", default="eval/baidu_standard_ab_report.md")
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--metric-k", type=int, default=10)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--qps-count", type=int, default=4)
    parser.add_argument("--skip-qps", action="store_true")
    parser.add_argument(
        "--retry-generation",
        action="store_true",
        help="账户恢复后重试已有引用但缺少原生答案的标准版请求",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    env = _read_project_env()
    if not settings.qianfan_api_key:
        raise ValueError("缺少 QIANFAN_API_KEY")
    if not settings.siliconflow_api_key:
        raise ValueError("缺少 SILICONFLOW_API_KEY")
    if not env.get("ANTHROPIC_API_KEY"):
        raise ValueError("缺少 ANTHROPIC_API_KEY")

    rows = _load_queries(args.dataset, args.queries)
    base_details = _load_json(Path(args.base_details), {})
    if not base_details.get("queries"):
        raise ValueError("缺少上一轮 baidu_highperf_ab_details.json")
    details_path = Path(args.details)
    details = _load_json(details_path, {
        "version": _CACHE_VERSION,
        "config": {
            "model": args.model,
            "search_source": "baidu_search_v2",
            "search_mode": "required",
            "enable_deep_search": False,
        },
        "queries": {},
        "qps_probe": [],
    })
    if details.get("version") != _CACHE_VERSION:
        raise ValueError("缓存版本不匹配")
    if details.get("config", {}).get("model") != args.model:
        raise ValueError("缓存模型与 --model 不一致，请使用新的 details 文件")

    session = requests.Session()
    try:
        for index, item in enumerate(rows, 1):
            query = item["query"]
            cached = details["queries"].setdefault(query, {"type": item.get("type", "")})
            existing = cached.get("standard", {})
            if (not existing.get("references")
                    or (args.retry_generation and not existing.get("answer"))):
                refreshed = _standard_search(
                    session,
                    settings.qianfan_api_key,
                    query,
                    args.model,
                    args.top_k,
                    stream=True,
                )
                cached["standard"] = refreshed
                # A retry performs a fresh search and can change candidates;
                # invalidate every downstream artifact derived from them.
                for key in (
                    "siliconflow_scores", "relevance_labels", "standard_answer",
                    "answer_judge", "native_answer_judge", "analysis",
                ):
                    cached.pop(key, None)
                _save_json(details_path, details)
                # The standard endpoint can finish the search leg while its
                # separately billed model leg is rate-limited.  Keep retries
                # below the documented default 1 QPS account limit.
                time.sleep(1.1)
            result = cached["standard"]
            print(
                f"[STANDARD {index:02d}/{len(rows)}] {query[:28]} "
                f"status={result.get('status')} refs={len(result.get('references') or [])} "
                f"answer={len(result.get('answer') or '')}chars "
                f"first_ref={result.get('first_reference_ms')}ms total={result.get('elapsed_ms')}ms",
                flush=True,
            )
            if not result.get("references"):
                raise RuntimeError(f"标准版请求失败，已写缓存: {query}: {result.get('error')}")
    finally:
        session.close()

    generation_block: dict[str, Any] | None = None
    for item in rows:
        result = details["queries"][item["query"]]["standard"]
        for error in result.get("embedded_errors") or []:
            if error.get("code") == "account_overdue":
                generation_block = dict(error)
                break
    if generation_block:
        details["generation_block"] = generation_block
    elif all(details["queries"][item["query"]]["standard"].get("answer") for item in rows):
        details.pop("generation_block", None)
    _save_json(details_path, details)

    reranker = SiliconFlowReranker(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model=settings.rerank_model,
        chunk_max_chars=settings.chunk_max_chars,
        chunk_overlap=settings.chunk_overlap,
    )
    for index, item in enumerate(rows, 1):
        query = item["query"]
        base = base_details["queries"][query]
        cached = details["queries"][query]
        pool = _dedup_refs((
            base["current"]["references"],
            base["high_full"]["references"],
            cached["standard"]["references"],
        ))
        if not cached.get("siliconflow_scores"):
            cached["siliconflow_scores"] = _score_union(reranker, query, pool)
            _save_json(details_path, details)
        print(f"[SF3 {index:02d}/{len(rows)}] {query[:32]} pool={len(pool)}", flush=True)

    claude = ClaudeClient(
        env["ANTHROPIC_API_KEY"],
        env.get("ANTHROPIC_BASE_URL", "").strip(),
        args.judge_model,
    )
    for index, item in enumerate(rows, 1):
        query = item["query"]
        base = base_details["queries"][query]
        cached = details["queries"][query]
        pool = _dedup_refs((
            base["current"]["references"],
            base["high_full"]["references"],
            cached["standard"]["references"],
        ))
        if not cached.get("relevance_labels"):
            cached["relevance_labels"] = _judge_relevance_three(claude, query, pool)
            _save_json(details_path, details)

        sf_scores = cached["siliconflow_scores"]
        current_refs = _rank_refs(base["current"]["references"], sf_scores)
        high_refs = _rank_refs(base["high_full"]["references"], None, baidu=True)
        standard_refs = _rank_refs(cached["standard"]["references"], sf_scores)
        if not cached.get("standard_answer"):
            cached["standard_answer"] = _baseline_answer(claude, query, standard_refs)
            _save_json(details_path, details)
        if not cached.get("answer_judge"):
            cached["answer_judge"] = _judge_three_answers(claude, query, {
                "current_sf": {
                    "answer": base.get("current_answer") or "",
                    "references": current_refs,
                },
                "highperf": {
                    "answer": base["high_full"].get("answer") or "",
                    "references": high_refs,
                },
                "standard_sf": {
                    "answer": cached["standard_answer"],
                    "references": standard_refs,
                },
            })
            _save_json(details_path, details)
        if cached["standard"].get("answer") and not cached.get("native_answer_judge"):
            cached["native_answer_judge"] = _judge_three_answers(claude, query, {
                "current_sf": {
                    "answer": base.get("current_answer") or "",
                    "references": current_refs,
                },
                "highperf": {
                    "answer": base["high_full"].get("answer") or "",
                    "references": high_refs,
                },
                "standard_native": {
                    "answer": cached["standard"]["answer"],
                    "references": _rank_refs(cached["standard"]["references"], None),
                },
            })
            _save_json(details_path, details)
        cached["analysis"] = _analyze_query(
            base,
            cached["standard"],
            cached["relevance_labels"],
            sf_scores,
            args.metric_k,
        )
        _save_json(details_path, details)
        print(
            f"[JUDGE3 {index:02d}/{len(rows)}] {query[:28]} "
            f"winner={cached['answer_judge']['winner']}",
            flush=True,
        )

    if (not args.skip_qps and not details.get("qps_probe")
            and not details.get("generation_block")):
        for rate in (1.0, 3.0, 5.0):
            stage = _run_qps_stage(
                settings.qianfan_api_key, args.model, rate, args.qps_count
            )
            details["qps_probe"].append(stage)
            _save_json(details_path, details)
            print(
                f"[QPS STANDARD] target={rate:g} "
                f"success={stage['successes']}/{stage['request_count']} "
                f"statuses={stage['statuses']}",
                flush=True,
            )
            time.sleep(2)

    summary = _summarize(base_details, details, rows, args.metric_k)
    details["summary"] = summary
    _save_json(details_path, details)
    Path(args.report).write_text(_report(summary, details, rows), encoding="utf-8")
    print(f"wrote {args.details}", flush=True)
    print(f"wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
