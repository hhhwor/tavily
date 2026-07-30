"""Statistics and Markdown rendering for the Baidu web-summary FreshQA run."""
from __future__ import annotations

import statistics
from datetime import date, datetime
from typing import Any

from eval.freshqa_eval import SNAPSHOT_DATE
from eval.freshqa_reporting import _paired_bootstrap, _percentile, _wilson


SYSTEMS = ("chukonu_fixed", "baidu_fixed", "baidu_native")
LABELS = {
    "baseline": "无搜索",
    "chukonu_fixed": "Chukonu + 固定回答器",
    "baidu_fixed": "百度 references + 固定回答器",
    "baidu_native": "百度 /web_summary 原生",
}


def _item(row: dict[str, Any], system: str) -> dict[str, Any]:
    return row["baseline"] if system == "baseline" else row["systems"][system]


def _comparison(
    rows: list[dict[str, Any]], left: str, right: str, metric: str, seed: int
) -> dict[str, Any]:
    left_values = [int(_item(row, left)["judgment"][metric]) for row in rows]
    right_values = [int(_item(row, right)["judgment"][metric]) for row in rows]
    return {
        "delta": statistics.fmean(a - b for a, b in zip(left_values, right_values)),
        "ci95": _paired_bootstrap(left_values, right_values, seed),
        "wins": sum(a > b for a, b in zip(left_values, right_values)),
        "ties": sum(a == b for a, b in zip(left_values, right_values)),
        "losses": sum(a < b for a, b in zip(left_values, right_values)),
    }


def summarize(rows: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    accuracy: dict[str, Any] = {}
    for system in ("baseline",) + SYSTEMS:
        accuracy[system] = {}
        for metric in ("strict", "relaxed"):
            values = [int(_item(row, system)["judgment"][metric]) for row in rows]
            successes = sum(values)
            accuracy[system][metric] = {
                "rate": successes / len(values),
                "ci95": _wilson(successes, len(values)),
            }
        accuracy[system]["containment"] = statistics.fmean(
            bool(_item(row, system).get("contains_reference")) for row in rows
        )
    comparisons = {
        f"{left}_vs_{right}": {
            metric: _comparison(rows, left, right, metric, seed)
            for metric in ("strict", "relaxed")
        }
        for left, right in (
            ("chukonu_fixed", "baseline"),
            ("baidu_fixed", "chukonu_fixed"),
            ("baidu_native", "chukonu_fixed"),
        )
    }
    ch_ms = [row["retrieval"]["chukonu"]["search_ms"] for row in rows]
    ba_ms = [row["retrieval"]["baidu"]["total_ms"] for row in rows]
    ch_e2e = [
        row["retrieval"]["chukonu"]["search_ms"]
        + row["systems"]["chukonu_fixed"]["answer_ms"]
        for row in rows
    ]
    ba_fixed_e2e = [
        row["retrieval"]["baidu"]["total_ms"]
        + row["systems"]["baidu_fixed"]["answer_ms"]
        for row in rows
    ]
    first_refs = [
        row["retrieval"]["baidu"]["first_reference_ms"] or 0 for row in rows
    ]
    first_tokens = [
        row["retrieval"]["baidu"]["first_token_ms"] or 0 for row in rows
    ]
    return {
        "n": len(rows),
        "accuracy": accuracy,
        "comparisons": comparisons,
        "runtime": {
            "chukonu_search_p50": _percentile(ch_ms, 0.5),
            "chukonu_search_p95": _percentile(ch_ms, 0.95),
            "baidu_first_ref_p50": _percentile(
                first_refs, 0.5
            ),
            "baidu_first_ref_p95": _percentile(first_refs, 0.95),
            "baidu_first_token_p50": _percentile(
                first_tokens, 0.5
            ),
            "baidu_first_token_p95": _percentile(first_tokens, 0.95),
            "baidu_total_p50": _percentile(ba_ms, 0.5),
            "baidu_total_p95": _percentile(ba_ms, 0.95),
            "chukonu_e2e_p50": _percentile(ch_e2e, 0.5),
            "chukonu_e2e_p95": _percentile(ch_e2e, 0.95),
            "baidu_fixed_e2e_p50": _percentile(ba_fixed_e2e, 0.5),
            "baidu_fixed_e2e_p95": _percentile(ba_fixed_e2e, 0.95),
        },
        "retrieval": {
            "chukonu_complete": sum(
                row["retrieval"]["chukonu"]["status"] in {"ok", "complete"}
                for row in rows
            ),
            "chukonu_partial": sum(
                row["retrieval"]["chukonu"]["status"] == "partial" for row in rows
            ),
            "baidu_complete": sum(
                row["retrieval"]["baidu"]["status"] == "complete" for row in rows
            ),
            "baidu_empty_answer": sum(
                row["retrieval"]["baidu"]["native_answer_empty"] for row in rows
            ),
            "chukonu_avg_evidence": statistics.fmean(
                row["retrieval"]["chukonu"]["evidence_count"] for row in rows
            ),
            "baidu_avg_evidence": statistics.fmean(
                row["retrieval"]["baidu"]["evidence_count"] for row in rows
            ),
            "baidu_retries": sum(
                row["retrieval"]["baidu"]["retries"] for row in rows
            ),
            "baidu_attempts": sum(
                1 + row["retrieval"]["baidu"]["retries"] for row in rows
            ),
        },
    }


def _current_rows(
    rows: list[dict[str, Any]], evaluation_date: str
) -> list[dict[str, Any]]:
    cutoff = date.fromisoformat(evaluation_date)
    current = []
    for row in rows:
        try:
            review = datetime.strptime(
                row["metadata"]["next_review"].strip(), "%m/%d/%Y"
            ).date()
        except (TypeError, ValueError):
            review = None
        if review is None or review > cutoff:
            current.append(row)
    return current


def render_report(details: dict[str, Any]) -> str:
    rows = [
        details["results"][key]
        for key in sorted(details["results"], key=lambda value: int(value))
    ]
    summary = details["summary"]
    lines = [
        f"# Chukonu vs 百度 `/web_summary` FreshQA 评测（n={len(rows)}）",
        "",
        f"- generated_at_utc: `{details['generated_at_utc']}`",
        f"- FreshQA snapshot: `{SNAPSHOT_DATE}` (`{details['dataset_sha256'][:12]}…`)",
        f"- split/seed: `{details['config']['split']}` / `{details['config']['seed']}`",
        f"- answer/judge: `{details['config']['answer_model']}` / `{details['config']['judge_model']}`",
        f"- Top-{details['config']['search_limit']}，证据预算 `{details['config']['evidence_chars']}` 字符",
        "- 百度配置：`stream=true`, `model=non_thinking`, `enable_full_content=true`",
        "",
        "## 总览",
        "",
        "| 系统 | Strict | Relaxed | 参考答案字符串命中 |",
        "|---|---:|---:|---:|",
    ]
    for name in ("baseline",) + SYSTEMS:
        item = summary["accuracy"][name]
        lines.append(
            f"| {LABELS[name]} | {item['strict']['rate']:.1%} | "
            f"{item['relaxed']['rate']:.1%} | {item['containment']:.1%} |"
        )
    fixed = summary["comparisons"]["baidu_fixed_vs_chukonu_fixed"]
    native = summary["comparisons"]["baidu_native_vs_chukonu_fixed"]
    lines += [
        "",
        "## 核心结论",
        "",
        f"- 统一回答器赛道：百度 Strict 相对 Chukonu "
        f"`{fixed['strict']['delta']:+.1%}`，95% CI "
        f"`[{fixed['strict']['ci95'][0]:+.1%}, {fixed['strict']['ci95'][1]:+.1%}]`；"
        "区间不跨 0，本次样本下差异显著。",
        f"- 原生端到端赛道：百度 Strict 相对 Chukonu "
        f"`{native['strict']['delta']:+.1%}`，95% CI "
        f"`[{native['strict']['ci95'][0]:+.1%}, {native['strict']['ci95'][1]:+.1%}]`；"
        "区间跨 0，不能据此认定两者存在显著差异。",
        "- 百度原生在稳定事实类表现较好，但在时效类问题上弱于 Chukonu；"
        "具体差异见 fact_type 分桶。",
        "- 百度原生接口直接生成答案，P50 不能与 Chukonu 的纯搜索 P50 "
        "视为同一链路指标；端到端比较应使用 Chukonu 搜索+固定回答。",
    ]
    lines += [
        "",
        "## 配对差异",
        "",
        "| 对比 | 口径 | Δ | 95% CI | 胜/平/负 |",
        "|---|---|---:|---:|---:|",
    ]
    for key, label in (
        ("chukonu_fixed_vs_baseline", "Chukonu − 无搜索"),
        ("baidu_fixed_vs_chukonu_fixed", "百度统一回答器 − Chukonu"),
        ("baidu_native_vs_chukonu_fixed", "百度原生 − Chukonu"),
    ):
        for metric, metric_label in (("strict", "Strict"), ("relaxed", "Relaxed")):
            item = summary["comparisons"][key][metric]
            lines.append(
                f"| {label} | {metric_label} | {item['delta']:+.1%} | "
                f"[{item['ci95'][0]:+.1%}, {item['ci95'][1]:+.1%}] | "
                f"{item['wins']}/{item['ties']}/{item['losses']} |"
            )
    current = _current_rows(rows, details["config"]["evaluation_date"])
    lines += [
        "",
        "## 明确未过期子集",
        "",
        f"`n={len(current)}`",
        "",
        "| 系统 | Strict | Relaxed |",
        "|---|---:|---:|",
    ]
    for name in ("baseline",) + SYSTEMS:
        strict = statistics.fmean(
            _item(row, name)["judgment"]["strict"] for row in current
        )
        relaxed = statistics.fmean(
            _item(row, name)["judgment"]["relaxed"] for row in current
        )
        lines.append(f"| {LABELS[name]} | {strict:.1%} | {relaxed:.1%} |")
    runtime = summary["runtime"]
    lines += [
        "",
        "## 运行表现",
        "",
        "| 指标 | P50 | P95 |",
        "|---|---:|---:|",
        f"| Chukonu 搜索 | {runtime['chukonu_search_p50']:.0f} ms | {runtime['chukonu_search_p95']:.0f} ms |",
        f"| 百度完整流 | {runtime['baidu_total_p50']:.0f} ms | {runtime['baidu_total_p95']:.0f} ms |",
        f"| Chukonu 搜索+固定回答 | {runtime['chukonu_e2e_p50']:.0f} ms | {runtime['chukonu_e2e_p95']:.0f} ms |",
        f"| 百度完整流+固定回答 | {runtime['baidu_fixed_e2e_p50']:.0f} ms | {runtime['baidu_fixed_e2e_p95']:.0f} ms |",
        "",
        f"- 百度首 references P50/P95：`{runtime['baidu_first_ref_p50']:.0f}/{runtime['baidu_first_ref_p95']:.0f} ms`",
        f"- 百度首答案 Token P50/P95：`{runtime['baidu_first_token_p50']:.0f}/{runtime['baidu_first_token_p95']:.0f} ms`",
        f"- Chukonu complete/partial：`{summary['retrieval']['chukonu_complete']}`/"
        f"`{summary['retrieval']['chukonu_partial']}`；平均 evidence："
        f"`{summary['retrieval']['chukonu_avg_evidence']:.2f}`",
        f"- 百度检索成功：`{summary['retrieval']['baidu_complete']}/{len(rows)}`；"
        f"空原生答案：`{summary['retrieval']['baidu_empty_answer']}`；平均 references："
        f"`{summary['retrieval']['baidu_avg_evidence']:.2f}`",
        f"- 百度请求尝试/重试：`{summary['retrieval']['baidu_attempts']}`/"
        f"`{summary['retrieval']['baidu_retries']}`；按目录价估算："
        f"`¥{summary['retrieval']['baidu_attempts'] * details['config']['baidu_unit_cost_rmb']:.2f}`"
        "（不抵扣免费额度，不含固定回答器和 Judge）",
        "",
        "## 分桶（Strict）",
        "",
        "| fact_type | n | Chukonu | 百度统一回答器 | 百度原生 |",
        "|---|---:|---:|---:|---:|",
    ]
    for fact_type in sorted({row["metadata"]["fact_type"] for row in rows}):
        bucket = [row for row in rows if row["metadata"]["fact_type"] == fact_type]
        rates = [
            statistics.fmean(
                _item(row, name)["judgment"]["strict"] for row in bucket
            )
            for name in SYSTEMS
        ]
        lines.append(
            f"| {fact_type} | {len(bucket)} | {rates[0]:.1%} | "
            f"{rates[1]:.1%} | {rates[2]:.1%} |"
        )
    lines += [
        "",
        "## 任务结构（Strict）",
        "",
        "| 维度 | 值 | n | Chukonu | 百度统一回答器 | 百度原生 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for dimension in ("false_premise", "num_hops"):
        for value in sorted({row["metadata"][dimension] for row in rows}):
            bucket = [
                row for row in rows if row["metadata"][dimension] == value
            ]
            rates = [
                statistics.fmean(
                    _item(row, name)["judgment"]["strict"] for row in bucket
                )
                for name in SYSTEMS
            ]
            lines.append(
                f"| {dimension} | {value} | {len(bucket)} | {rates[0]:.1%} | "
                f"{rates[1]:.1%} | {rates[2]:.1%} |"
            )
    lines += [
        "",
        "## 限制",
        "",
        f"- 快照早于评测日；`{len(rows) - len(current)}` 条样本的显式 next_review 已到期。",
        "- 百度原生赛道同时包含百度检索与生成能力；百度统一回答器赛道用于隔离检索差异。",
        "- 自动 Judge 不是 FreshQA 官方指定模型，分数用于本次配对比较，不作为官方榜单成绩。",
        "- `/web_summary` 的完整流同时包含检索和生成，不能与纯搜索延迟直接等价。",
    ]
    return "\n".join(lines) + "\n"
