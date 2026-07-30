"""Statistics and Markdown rendering for the Aliyun paired FreshQA run."""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from eval.freshqa_eval import SNAPSHOT_DATE
from eval.freshqa_reporting import _paired_bootstrap, _percentile, _wilson


SYSTEMS = ("three_source", "four_source")
LABELS = {
    "three_source": "当前三源",
    "four_source": "加入 Aliyun 四源",
}


def _comparison(
    rows: list[dict[str, Any]],
    metric: str,
    seed: int,
) -> dict[str, Any]:
    four = [
        int(row["systems"]["four_source"]["judgment"][metric])
        for row in rows
    ]
    three = [
        int(row["systems"]["three_source"]["judgment"][metric])
        for row in rows
    ]
    return {
        "delta": statistics.fmean(a - b for a, b in zip(four, three)),
        "ci95": _paired_bootstrap(four, three, seed),
        "wins": sum(a > b for a, b in zip(four, three)),
        "ties": sum(a == b for a, b in zip(four, three)),
        "losses": sum(a < b for a, b in zip(four, three)),
    }


def _source_mix(
    rows: list[dict[str, Any]],
    system: str,
) -> dict[str, dict[str, int]]:
    labels: Counter[str] = Counter()
    credits: Counter[str] = Counter()
    queries: Counter[str] = Counter()
    for row in rows:
        present: set[str] = set()
        for evidence in row["retrieval"][system]["evidence"]:
            source = str(evidence.get("source") or "unknown")
            labels[source] += 1
            providers = {item for item in source.split("+") if item}
            credits.update(providers)
            present.update(providers)
        queries.update(present)
    return {
        "evidence_labels": dict(labels),
        "provider_evidence_credits": dict(credits),
        "queries_with_provider": dict(queries),
    }


def summarize(
    rows: list[dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    accuracy: dict[str, Any] = {}
    retrieval: dict[str, Any] = {}
    for system in SYSTEMS:
        accuracy[system] = {}
        for metric in ("strict", "relaxed"):
            values = [
                int(row["systems"][system]["judgment"][metric])
                for row in rows
            ]
            successes = sum(values)
            accuracy[system][metric] = {
                "rate": successes / len(values),
                "ci95": _wilson(successes, len(values)),
            }
        accuracy[system]["containment"] = statistics.fmean(
            bool(row["systems"][system]["contains_reference"])
            for row in rows
        )
        search_ms = [
            row["retrieval"][system]["search_ms"] for row in rows
        ]
        retrieval[system] = {
            "success_rate": statistics.fmean(
                row["retrieval"][system]["status"]
                in {"ok", "complete", "partial"}
                for row in rows
            ),
            "complete_rate": statistics.fmean(
                row["retrieval"][system]["status"] in {"ok", "complete"}
                for row in rows
            ),
            "partial_rate": statistics.fmean(
                row["retrieval"][system]["status"] == "partial"
                for row in rows
            ),
            "provider_failure_rate": statistics.fmean(
                bool(row["retrieval"][system]["failures"])
                for row in rows
            ),
            "avg_evidence": statistics.fmean(
                row["retrieval"][system]["evidence_count"]
                for row in rows
            ),
            "p50_ms": _percentile(search_ms, 0.5),
            "p95_ms": _percentile(search_ms, 0.95),
            "endpoint_retries": sum(
                row["retrieval"][system]["retries"] for row in rows
            ),
            "failures": dict(Counter(
                f"{failure.get('source', 'unknown')}:"
                f"{failure.get('code', 'unknown')}"
                for row in rows
                for failure in row["retrieval"][system]["failures"]
            )),
            "source_mix": _source_mix(rows, system),
        }
    return {
        "n": len(rows),
        "accuracy": accuracy,
        "comparison": {
            metric: _comparison(rows, metric, seed)
            for metric in ("strict", "relaxed")
        },
        "retrieval": retrieval,
    }


def _bucket_rate(
    rows: list[dict[str, Any]],
    system: str,
) -> float:
    return statistics.fmean(
        row["systems"][system]["judgment"]["strict"] for row in rows
    )


def render_report(details: dict[str, Any]) -> str:
    rows = [
        details["results"][key]
        for key in sorted(details["results"], key=lambda value: int(value))
    ]
    summary = details["summary"]
    lines = [
        f"# FreshQA 配对评测：当前三源 vs 加入 Aliyun 四源（n={len(rows)}）",
        "",
        f"- generated_at_utc: `{details['generated_at_utc']}`",
        f"- FreshQA snapshot: `{SNAPSHOT_DATE}` "
        f"(`{details['dataset_sha256'][:12]}…`)",
        f"- split/seed: `{details['config']['split']}` / "
        f"`{details['config']['seed']}`",
        f"- answer/judge: `{details['config']['answer_model']}` / "
        f"`{details['config']['judge_model']}`",
        f"- Top-{details['config']['search_limit']}，证据预算 "
        f"`{details['config']['evidence_chars']}` 字符",
        "- 三源：Tencent + Baidu + Doubao；四源：三源 + Aliyun "
        f"WebSearch `{details['config']['aliyun_search_type']}` / "
        f"`{details['config']['aliyun_region']}`",
        "- 两个无缓存实例并行检索；同题、同回答器、同 Judge 配对评分。",
        "",
        "## 结果",
        "",
        "| 系统 | Strict | Relaxed | 参考答案字符串命中 |",
        "|---|---:|---:|---:|",
    ]
    for system in SYSTEMS:
        item = summary["accuracy"][system]
        lines.append(
            f"| {LABELS[system]} | {item['strict']['rate']:.1%} | "
            f"{item['relaxed']['rate']:.1%} | "
            f"{item['containment']:.1%} |"
        )
    lines += [
        "",
        "## 配对差异（四源 − 三源）",
        "",
        "| 口径 | Δ | 95% bootstrap CI | 胜/平/负 |",
        "|---|---:|---:|---:|",
    ]
    for metric, label in (("strict", "Strict"), ("relaxed", "Relaxed")):
        item = summary["comparison"][metric]
        lines.append(
            f"| {label} | {item['delta']:+.1%} | "
            f"[{item['ci95'][0]:+.1%}, {item['ci95'][1]:+.1%}] | "
            f"{item['wins']}/{item['ties']}/{item['losses']} |"
        )
    strict = summary["comparison"]["strict"]
    relaxed = summary["comparison"]["relaxed"]
    three_p50 = summary["retrieval"]["three_source"]["p50_ms"]
    four_p50 = summary["retrieval"]["four_source"]["p50_ms"]
    latency_delta = four_p50 / three_p50 - 1
    lines += [
        "",
        "## 结论与决策",
        "",
        f"- 四源 Strict 相对三源 `{strict['delta']:+.1%}`，配对胜负 "
        f"`{strict['wins']}/{strict['losses']}`；没有净准确率提升。",
        f"- Relaxed `{relaxed['delta']:+.1%}`，但 95% CI "
        f"`[{relaxed['ci95'][0]:+.1%}, {relaxed['ci95'][1]:+.1%}]` 跨 0，"
        "不能认定为稳定收益。",
        f"- 四源检索 P50 增加 `{latency_delta:+.1%}`，并新增按目录价约 "
        f"`¥{len(rows) * details['config']['aliyun_unit_cost_rmb']:.2f}` 的"
        " Aliyun 搜索成本。",
        "- 决策建议：暂不把 Aliyun 直接设为默认第四源，保持显式开关；"
        "下一轮改测稳定事实条件召回，或给 Aliyun 设置每题 1–2 条的融合配额，"
        "避免挤出既有源。",
    ]
    lines += [
        "",
        "## 检索运行",
        "",
        "| 系统 | complete | partial | provider failure | "
        "平均 evidence | P50 | P95 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for system in SYSTEMS:
        item = summary["retrieval"][system]
        lines.append(
            f"| {LABELS[system]} | {item['complete_rate']:.1%} | "
            f"{item['partial_rate']:.1%} | "
            f"{item['provider_failure_rate']:.1%} | "
            f"{item['avg_evidence']:.2f} | {item['p50_ms']:.0f} ms | "
            f"{item['p95_ms']:.0f} ms |"
        )
    four_mix = summary["retrieval"]["four_source"]["source_mix"]
    aliyun_queries = four_mix["queries_with_provider"].get("aliyun", 0)
    aliyun_credits = four_mix["provider_evidence_credits"].get("aliyun", 0)
    estimated_cost = (
        len(rows) * details["config"]["aliyun_unit_cost_rmb"]
    )
    lines += [
        "",
        f"- 四源最终 Top-{details['config']['search_limit']} 中含 Aliyun "
        f"evidence 的问题：`{aliyun_queries}/{len(rows)}`；Aliyun evidence "
        f"贡献计数：`{aliyun_credits}`。",
        f"- Aliyun Pro 最低调用量按每题 1 次计：`{len(rows)}`；目录价估算 "
        f"`¥{estimated_cost:.2f}`（不含内部重试、回答器和 Judge）。",
        f"- 三源 failures：`{summary['retrieval']['three_source']['failures']}`",
        f"- 四源 failures：`{summary['retrieval']['four_source']['failures']}`",
        "",
        "## 分桶（Strict）",
        "",
        "| fact_type | n | 当前三源 | 四源 | Δ |",
        "|---|---:|---:|---:|---:|",
    ]
    for fact_type in sorted({
        row["metadata"]["fact_type"] for row in rows
    }):
        bucket = [
            row for row in rows
            if row["metadata"]["fact_type"] == fact_type
        ]
        three = _bucket_rate(bucket, "three_source")
        four = _bucket_rate(bucket, "four_source")
        lines.append(
            f"| {fact_type} | {len(bucket)} | {three:.1%} | "
            f"{four:.1%} | {four - three:+.1%} |"
        )
    lines += [
        "",
        "## 限制",
        "",
        "- 这是同批配对比较，置信区间反映该 100 题样本上的随机不确定性；"
        "区间跨 0 时不能认定存在稳定提升。",
        "- 两个检索实例运行在同一主机且同步发起请求，绝对延迟可能受资源竞争影响；"
        "应优先看同批相对差异。",
        "- 自动 Judge 不是 FreshQA 官方指定模型，结果用于本地引擎选源决策，"
        "不作为官方榜单成绩。",
    ]
    return "\n".join(lines) + "\n"
