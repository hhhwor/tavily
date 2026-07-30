"""Statistics and Markdown rendering for the FreshQA runner."""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from datetime import date, datetime
from typing import Any


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _wilson(successes: int, total: int) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    z = 1.959963984540054
    observed = successes / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(observed * (1 - observed) / total + z * z / (4 * total * total))
        / denominator
    )
    return center - margin, center + margin


def _paired_bootstrap(
    engine: list[int], baseline: list[int], seed: int, repetitions: int = 10_000
) -> tuple[float, float]:
    if not engine:
        return 0.0, 0.0
    differences = [left - right for left, right in zip(engine, baseline)]
    rng = random.Random(seed)
    samples = [
        statistics.fmean(rng.choice(differences) for _ in differences)
        for _ in range(repetitions)
    ]
    samples.sort()
    return samples[int(0.025 * repetitions)], samples[int(0.975 * repetitions)]


def _parse_review_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for metric in ("strict", "relaxed"):
        engine_values = [int(row["engine"]["judgment"][metric]) for row in rows]
        baseline_values = [int(row["baseline"]["judgment"][metric]) for row in rows]
        total = len(engine_values)
        engine_success = sum(engine_values)
        baseline_success = sum(baseline_values)
        output[metric] = {
            "n": total,
            "engine": engine_success / total if total else 0,
            "baseline": baseline_success / total if total else 0,
            "absolute_uplift": (engine_success - baseline_success) / total if total else 0,
            "engine_ci95": _wilson(engine_success, total),
            "baseline_ci95": _wilson(baseline_success, total),
            "uplift_ci95": _paired_bootstrap(engine_values, baseline_values, seed),
            "wins": sum(left > right for left, right in zip(engine_values, baseline_values)),
            "ties": sum(left == right for left, right in zip(engine_values, baseline_values)),
            "losses": sum(left < right for left, right in zip(engine_values, baseline_values)),
        }
    output["containment"] = {
        side: statistics.fmean(bool(row[side]["contains_reference"]) for row in rows)
        for side in ("engine", "baseline")
    }
    output["search"] = {
        "success_rate": statistics.fmean(
            row["engine"].get("status") in {"ok", "complete", "partial"} for row in rows
        ),
        "complete_rate": statistics.fmean(
            row["engine"].get("status") in {"ok", "complete"} for row in rows
        ),
        "partial_rate": statistics.fmean(
            row["engine"].get("status") == "partial" for row in rows
        ),
        "provider_failure_rate": statistics.fmean(
            bool(row["engine"].get("failures")) for row in rows
        ),
        "avg_evidence": statistics.fmean(
            row["engine"].get("evidence_count", 0) for row in rows
        ),
        "p50_ms": _percentile([row["engine"]["search_ms"] for row in rows], 0.5),
        "p95_ms": _percentile([row["engine"]["search_ms"] for row in rows], 0.95),
        "failures": dict(Counter(
            f"{failure.get('source', 'unknown')}:{failure.get('code', 'unknown')}"
            for row in rows
            for failure in row["engine"].get("failures", [])
        )),
    }
    return output


def render_report(
    details: dict[str, Any], *, snapshot_date: str
) -> str:
    rows = [
        details["results"][key]
        for key in sorted(details["results"], key=lambda item: int(item))
    ]
    summary = details["summary"]
    engine_label = details["config"].get("engine_label", "Chukonu")
    engine_endpoint = details["config"].get(
        "search_url", details["config"].get("search_backend", "unknown")
    )
    lines = [
        f"# {engine_label} FreshQA 评测（n={len(rows)}）",
        "",
        f"- generated_at_utc: `{details['generated_at_utc']}`",
        f"- FreshQA snapshot: `{snapshot_date}` (`{details['dataset_sha256'][:12]}…`)",
        f"- split/sample: `{details['config']['split']}` / seed `{details['config']['seed']}`",
        f"- answer model: `{details['config']['answer_model']}`",
        f"- judge model: `{details['config']['judge_model']}`",
        f"- engine: `{engine_endpoint}`, Top-{details['config']['search_limit']}",
        f"- official repo commit: `{details['config']['official_repo_commit']}`",
        "",
        "## 总览",
        "",
        f"| 口径 | 无搜索 | {engine_label} | 绝对提升 | 95% CI | 胜/平/负 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, label in (("strict", "严格"), ("relaxed", "宽松")):
        item = summary[key]
        lines.append(
            f"| {label} | {item['baseline']:.1%} | {item['engine']:.1%} | "
            f"{item['absolute_uplift']:+.1%} | "
            f"[{item['uplift_ci95'][0]:+.1%}, {item['uplift_ci95'][1]:+.1%}] | "
            f"{item['wins']}/{item['ties']}/{item['losses']} |"
        )
    lines += [
        f"| 参考答案字符串命中 | {summary['containment']['baseline']:.1%} | "
        f"{summary['containment']['engine']:.1%} | "
        f"{summary['containment']['engine'] - summary['containment']['baseline']:+.1%} | - | - |",
        "",
        "## 检索运行",
        "",
        f"- 成功率：`{summary['search']['success_rate']:.1%}`；partial：`{summary['search']['partial_rate']:.1%}`",
        f"- complete：`{summary['search']['complete_rate']:.1%}`",
        f"- 至少一个 provider failure：`{summary['search']['provider_failure_rate']:.1%}`",
        f"- provider failures：`{summary['search']['failures']}`",
        f"- 平均 evidence：`{summary['search']['avg_evidence']:.2f}`",
        f"- 搜索延迟 P50/P95：`{summary['search']['p50_ms']:.0f}/{summary['search']['p95_ms']:.0f} ms`",
        "",
        "## 分桶（严格）",
        "",
        f"| fact_type | n | 无搜索 | {engine_label} | Δ |",
        "|---|---:|---:|---:|---:|",
    ]
    for fact_type in sorted({row["metadata"]["fact_type"] for row in rows}):
        bucket = [row for row in rows if row["metadata"]["fact_type"] == fact_type]
        engine = statistics.fmean(row["engine"]["judgment"]["strict"] for row in bucket)
        baseline = statistics.fmean(row["baseline"]["judgment"]["strict"] for row in bucket)
        lines.append(
            f"| {fact_type} | {len(bucket)} | {baseline:.1%} | "
            f"{engine:.1%} | {engine - baseline:+.1%} |"
        )
    overdue = [
        row
        for row in rows
        if (_parse_review_date(row["metadata"]["next_review"]) or date.max)
        <= date.fromisoformat(details["config"]["evaluation_date"])
    ]
    current_rows = [row for row in rows if row not in overdue]
    lines += [
        "",
        "## 明确未过期子集",
        "",
        f"| 口径 | n | 无搜索 | {engine_label} | Δ |",
        "|---|---:|---:|---:|---:|",
    ]
    for metric, label in (("strict", "严格"), ("relaxed", "宽松")):
        baseline = statistics.fmean(
            row["baseline"]["judgment"][metric] for row in current_rows
        )
        engine = statistics.fmean(
            row["engine"]["judgment"][metric] for row in current_rows
        )
        lines.append(
            f"| {label} | {len(current_rows)} | {baseline:.1%} | "
            f"{engine:.1%} | {engine - baseline:+.1%} |"
        )
    lines += [
        "",
        "## 任务结构（严格）",
        "",
        f"| 维度 | 值 | n | 无搜索 | {engine_label} | Δ |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for dimension in ("false_premise", "num_hops"):
        for value in sorted({row["metadata"][dimension] for row in rows}):
            bucket = [row for row in rows if row["metadata"][dimension] == value]
            baseline = statistics.fmean(
                row["baseline"]["judgment"]["strict"] for row in bucket
            )
            engine = statistics.fmean(
                row["engine"]["judgment"]["strict"] for row in bucket
            )
            lines.append(
                f"| {dimension} | {value} | {len(bucket)} | {baseline:.1%} | "
                f"{engine:.1%} | {engine - baseline:+.1%} |"
            )
    lines += [
        "",
        "## 限制",
        "",
        f"- 官方最新可获取快照早于评测日；样本中有 `{len(overdue)}` 条显式 `next_review` 日期已过。",
        "- 自动评分沿用 FreshEval 的严格/宽松原则，但 Judge 不是官方推荐的 GPT-4-1106-preview，分数不可直接用于官方榜单。",
        "- 无搜索与搜索组使用同一回答模型；这能估计搜索增益，但结果仍包含回答模型能力。",
        "- 参考答案字符串命中只作确定性辅助指标，会漏记合法改写。",
        "",
        "## 失败样本（严格）",
        "",
        f"| ID | Question | Baseline | {engine_label} |",
        "|---:|---|---:|---:|",
    ]
    for row in [
        item for item in rows if not item["engine"]["judgment"]["strict"]
    ][:20]:
        question = row["question"].replace("|", "\\|")
        lines.append(
            f"| {row['id']} | {question} | "
            f"{int(row['baseline']['judgment']['strict'])} | "
            f"{int(row['engine']['judgment']['strict'])} |"
        )
    return "\n".join(lines) + "\n"
