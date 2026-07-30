"""Offline comparison of aggregating the three Baidu AI-search endpoints.

The evaluation reuses the 2026-07-23 same-window snapshots and relevance/SF
scores produced by ``baidu_standard_ab.py``.  It performs no network calls.

Compared configurations:
  - each source alone;
  - every two-source combination;
  - all three sources;
  - RRF fusion and SiliconFlow semantic reranking.
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from eval.baidu_highperf_ab import (
    _dedup_refs,
    _load_json,
    _load_queries,
    _mean_dict,
    _metrics_for,
    _paired_bootstrap,
    _percentile,
    _rank_refs,
    _ref_key,
    _ref_text,
    _save_json,
)


SOURCE_ORDER = ("current", "highperf", "standard")
SOURCE_LABELS = {
    "current": "当前百度搜索",
    "highperf": "高性能版",
    "standard": "标准版",
}
COMBINATIONS = (
    ("current",),
    ("highperf",),
    ("standard",),
    ("current", "highperf"),
    ("current", "standard"),
    ("highperf", "standard"),
    SOURCE_ORDER,
)
SEARCH_COST_RMB = {
    "current": 0.036,
    "highperf": 0.060,
    "standard": 0.036,
}


def _combo_id(sources: Sequence[str]) -> str:
    return "+".join(sources) if len(sources) < 3 else "all_three"


def _combo_label(sources: Sequence[str]) -> str:
    return " + ".join(SOURCE_LABELS[source] for source in sources)


def _source_refs(
    base_query: dict[str, Any],
    standard_query: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    # The high-performance endpoint exposes an explicit Baidu rerank score.
    # Use it as that source's internal order before RRF.
    return {
        "current": _rank_refs(base_query["current"]["references"], None),
        "highperf": _rank_refs(
            base_query["high_full"]["references"], None, baidu=True
        ),
        "standard": _rank_refs(standard_query["standard"]["references"], None),
    }


def _rrf_rank(
    refs_by_source: dict[str, Sequence[dict[str, Any]]],
    sources: Sequence[str],
    k_rrf: int,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    sequence = 0
    for source in sources:
        for rank, ref in enumerate(refs_by_source[source]):
            key = _ref_key(ref, rank)
            contribution = 1.0 / (k_rrf + rank + 1)
            if key not in groups:
                groups[key] = {
                    "ref": dict(ref),
                    "score": contribution,
                    "best_rank": rank,
                    "first_seen": sequence,
                    "sources": [source],
                }
                sequence += 1
                continue
            row = groups[key]
            row["score"] += contribution
            row["best_rank"] = min(row["best_rank"], rank)
            row["sources"].append(source)
            if len(_ref_text(ref)) > len(_ref_text(row["ref"])):
                row["ref"] = dict(ref)

    ordered = sorted(
        groups.values(),
        key=lambda row: (
            -row["score"],
            row["best_rank"],
            row["first_seen"],
            _ref_key(row["ref"]),
        ),
    )
    return [row["ref"] for row in ordered]


def _sf_rank(
    refs_by_source: dict[str, Sequence[dict[str, Any]]],
    sources: Sequence[str],
    scores: dict[str, float],
) -> list[dict[str, Any]]:
    union = _dedup_refs(refs_by_source[source] for source in sources)
    return sorted(
        union,
        key=lambda ref: scores.get(_ref_key(ref), -1.0),
        reverse=True,
    )


def _source_ready_ms(
    base_query: dict[str, Any],
    standard_query: dict[str, Any],
) -> dict[str, float]:
    return {
        "current": float(base_query["current"]["elapsed_ms"]),
        "highperf": float(base_query["high_full"]["first_reference_ms"]),
        "standard": float(standard_query["standard"]["first_reference_ms"]),
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left or right else 0.0


def _query_analysis(
    base_query: dict[str, Any],
    standard_query: dict[str, Any],
    metric_k: int,
    k_rrf: int,
) -> dict[str, Any]:
    refs_by_source = _source_refs(base_query, standard_query)
    labels = {
        str(key): int(value)
        for key, value in standard_query["relevance_labels"].items()
    }
    sf_scores = {
        str(key): float(value)
        for key, value in standard_query["siliconflow_scores"].items()
    }
    global_pool = _dedup_refs(refs_by_source[source] for source in SOURCE_ORDER)
    global_keys = {_ref_key(ref, index) for index, ref in enumerate(global_pool)}
    relevant_global = {key for key in global_keys if labels.get(key, 0) >= 2}
    source_keys = {
        source: {
            _ref_key(ref, index)
            for index, ref in enumerate(refs_by_source[source])
        }
        for source in SOURCE_ORDER
    }
    source_ready = _source_ready_ms(base_query, standard_query)

    combos: dict[str, Any] = {}
    for sources in COMBINATIONS:
        combo = _combo_id(sources)
        union = _dedup_refs(refs_by_source[source] for source in sources)
        union_keys = {_ref_key(ref, index) for index, ref in enumerate(union)}
        total_input = sum(len(refs_by_source[source]) for source in sources)
        rankings = {
            "rrf": _rrf_rank(refs_by_source, sources, k_rrf),
            "sf": _sf_rank(refs_by_source, sources, sf_scores),
        }
        source_exposure: dict[str, float] = {}
        sf_top_keys = {
            _ref_key(ref, index)
            for index, ref in enumerate(rankings["sf"][:metric_k])
        }
        for source in sources:
            source_exposure[source] = float(len(sf_top_keys & source_keys[source]))

        current_keys = source_keys["current"]
        relevant_union = union_keys & relevant_global
        combos[combo] = {
            "sources": list(sources),
            "candidate_count": len(union),
            "input_count": total_input,
            "dedup_rate": (
                1.0 - len(union) / total_input if total_input else 0.0
            ),
            "candidate_relevant_coverage": (
                len(relevant_union) / len(relevant_global)
                if relevant_global
                else 0.0
            ),
            "urls_added_vs_current": len(union_keys - current_keys),
            "relevant_added_vs_current": len(
                (union_keys - current_keys) & relevant_global
            ),
            "parallel_source_ready_ms": max(source_ready[source] for source in sources),
            "source_exposure_at_k": source_exposure,
            "metrics": {
                strategy: _metrics_for(ranked, labels, global_pool, metric_k)
                for strategy, ranked in rankings.items()
            },
        }

    pair_jaccard = {}
    for left, right in itertools.combinations(SOURCE_ORDER, 2):
        pair_jaccard[f"{left}+{right}"] = _jaccard(
            source_keys[left], source_keys[right]
        )

    exclusive_relevant = {}
    for source in SOURCE_ORDER:
        other_keys: set[str] = set()
        for other in SOURCE_ORDER:
            if other != source:
                other_keys |= source_keys[other]
        exclusive_relevant[source] = len(
            (source_keys[source] - other_keys) & relevant_global
        )

    return {
        "global_pool_count": len(global_pool),
        "global_relevant_count": len(relevant_global),
        "pair_jaccard": pair_jaccard,
        "exclusive_relevant": exclusive_relevant,
        "combos": combos,
    }


def _summarize(
    query_rows: dict[str, dict[str, Any]],
    metric_k: int,
    k_rrf: int,
    standard_model_cost_mean_rmb: float,
    source_snapshot_utc: str | None,
) -> dict[str, Any]:
    combo_summary: dict[str, Any] = {}
    query_values = list(query_rows.values())
    for sources in COMBINATIONS:
        combo = _combo_id(sources)
        rows = [query["combos"][combo] for query in query_values]
        combo_summary[combo] = {
            "sources": list(sources),
            "label": _combo_label(sources),
            "candidate_count_mean": statistics.fmean(
                row["candidate_count"] for row in rows
            ),
            "dedup_rate_mean": statistics.fmean(
                row["dedup_rate"] for row in rows
            ),
            "candidate_relevant_coverage_mean": statistics.fmean(
                row["candidate_relevant_coverage"] for row in rows
            ),
            "urls_added_vs_current_mean": statistics.fmean(
                row["urls_added_vs_current"] for row in rows
            ),
            "relevant_added_vs_current_mean": statistics.fmean(
                row["relevant_added_vs_current"] for row in rows
            ),
            "parallel_source_ready_p50_ms": _percentile(
                [row["parallel_source_ready_ms"] for row in rows], 0.5
            ),
            "parallel_source_ready_p95_ms": _percentile(
                [row["parallel_source_ready_ms"] for row in rows], 0.95
            ),
            "directory_cost_rmb": sum(
                SEARCH_COST_RMB[source] for source in sources
            ) + (
                standard_model_cost_mean_rmb if "standard" in sources else 0.0
            ),
            "metrics": {
                strategy: _mean_dict(
                    [row["metrics"][strategy] for row in rows]
                )
                for strategy in ("rrf", "sf")
            },
            "source_exposure_at_k_mean": {
                source: statistics.fmean(
                    row["source_exposure_at_k"].get(source, 0.0)
                    for row in rows
                )
                for source in sources
            },
        }

    baseline = "current"
    paired_checks: dict[str, Any] = {}
    for sources in COMBINATIONS[3:]:
        combo = _combo_id(sources)
        for metric in ("ndcg", "recall"):
            differences = [
                query["combos"][combo]["metrics"]["sf"][metric]
                - query["combos"][baseline]["metrics"]["sf"][metric]
                for query in query_values
            ]
            paired_checks[f"{combo}_sf_minus_current_sf_{metric}"] = (
                _paired_bootstrap(
                    differences,
                    seed=20260810 + len(paired_checks),
                )
            )

    pair_jaccard = {
        pair: statistics.fmean(
            query["pair_jaccard"][pair] for query in query_values
        )
        for pair in ("current+highperf", "current+standard", "highperf+standard")
    }
    exclusive_relevant = {
        source: {
            "total": sum(
                query["exclusive_relevant"][source] for query in query_values
            ),
            "mean_per_query": statistics.fmean(
                query["exclusive_relevant"][source] for query in query_values
            ),
        }
        for source in SOURCE_ORDER
    }
    return {
        "generated_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "source_snapshot_utc": source_snapshot_utc,
        "query_count": len(query_rows),
        "metric_k": metric_k,
        "rrf_k": k_rrf,
        "standard_model_cost_mean_rmb": standard_model_cost_mean_rmb,
        "combo_summary": combo_summary,
        "paired_checks": paired_checks,
        "pair_jaccard": pair_jaccard,
        "exclusive_relevant": exclusive_relevant,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _report(summary: dict[str, Any]) -> str:
    combos = summary["combo_summary"]
    lines = [
        "# 百度三接口多源聚合离线对比",
        "",
        f"- 数据快照：`{summary['source_snapshot_utc']}`",
        f"- Query：`{summary['query_count']}`；指标：`@{summary['metric_k']}`；RRF k：`{summary['rrf_k']}`",
        "- 数据来自同时间窗口的三个百度接口缓存；未发起新的网络请求。",
        "- 目录价成本不抵扣免费额度；含标准版时，模型成本采用 ERNIE 4.5 复测均值。",
        "",
        "## 候选规模、覆盖、延迟与成本",
        "",
        "| 配置 | 去重候选/Query | 去重率 | 相关候选覆盖 | 较当前新增相关/Query | 并行源就绪 P50/P95 | 目录价/Query |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sources in COMBINATIONS:
        combo = _combo_id(sources)
        row = combos[combo]
        lines.append(
            f"| {row['label']} | {_fmt(row['candidate_count_mean'], 1)} | "
            f"{_fmt(row['dedup_rate_mean'])} | "
            f"{_fmt(row['candidate_relevant_coverage_mean'])} | "
            f"{_fmt(row['relevant_added_vs_current_mean'], 2)} | "
            f"{_fmt(row['parallel_source_ready_p50_ms'] / 1000, 3)}/"
            f"{_fmt(row['parallel_source_ready_p95_ms'] / 1000, 3)}s | "
            f"¥{_fmt(row['directory_cost_rmb'], 4)} |"
        )

    for strategy, title in (
        ("rrf", "RRF 聚合"),
        ("sf", "SiliconFlow 聚合重排"),
    ):
        lines += [
            "",
            f"## {title}",
            "",
            "| 配置 | nDCG@10 | Recall@10 | P@10 | MRR |",
            "|---|---:|---:|---:|---:|",
        ]
        for sources in COMBINATIONS:
            combo = _combo_id(sources)
            row = combos[combo]["metrics"][strategy]
            lines.append(
                f"| {combos[combo]['label']} | {_fmt(row['ndcg'])} | "
                f"{_fmt(row['recall'])} | {_fmt(row['precision'])} | "
                f"{_fmt(row['mrr'])} |"
            )

    lines += [
        "",
        "## 聚合 + SF 相比当前单源 + SF",
        "",
        "| 聚合 | ΔnDCG（95% CI） | 胜/平/负 | ΔRecall（95% CI） | 胜/平/负 |",
        "|---|---:|---:|---:|---:|",
    ]
    for sources in COMBINATIONS[3:]:
        combo = _combo_id(sources)
        ndcg = summary["paired_checks"][
            f"{combo}_sf_minus_current_sf_ndcg"
        ]
        recall = summary["paired_checks"][
            f"{combo}_sf_minus_current_sf_recall"
        ]
        lines.append(
            f"| {combos[combo]['label']} | "
            f"{_fmt(ndcg['mean_delta'])} "
            f"`[{_fmt(ndcg['ci95'][0])}, {_fmt(ndcg['ci95'][1])}]` | "
            f"`{ndcg['wins_ties_losses']}` | "
            f"{_fmt(recall['mean_delta'])} "
            f"`[{_fmt(recall['ci95'][0])}, {_fmt(recall['ci95'][1])}]` | "
            f"`{recall['wins_ties_losses']}` |"
        )

    lines += [
        "",
        "## 来源互补性",
        "",
        "| 来源对 | URL Jaccard |",
        "|---|---:|",
    ]
    for pair, value in summary["pair_jaccard"].items():
        lines.append(f"| {pair} | {_fmt(value)} |")
    lines += [
        "",
        "| 来源 | 仅该源命中的相关文档总数 | 每 Query 均值 |",
        "|---|---:|---:|",
    ]
    for source in SOURCE_ORDER:
        row = summary["exclusive_relevant"][source]
        lines.append(
            f"| {SOURCE_LABELS[source]} | {row['total']} | "
            f"{_fmt(row['mean_per_query'], 2)} |"
        )

    sf_rows = {
        combo: row["metrics"]["sf"] for combo, row in combos.items()
    }
    best_ndcg = max(sf_rows, key=lambda name: sf_rows[name]["ndcg"])
    best_recall = max(sf_rows, key=lambda name: sf_rows[name]["recall"])
    lines += [
        "",
        "## 自动摘要",
        "",
        f"- SF 方案最高 nDCG：`{combos[best_ndcg]['label']}` "
        f"`{_fmt(sf_rows[best_ndcg]['ndcg'])}`。",
        f"- SF 方案最高 Recall：`{combos[best_recall]['label']}` "
        f"`{_fmt(sf_rows[best_recall]['recall'])}`。",
        "- 绝对 Recall 基于三接口 Top-20 并集池，只适合本组配置横向比较。",
        "- 延迟仅统计各源候选就绪时间，不含 SiliconFlow 调用、网络排队和答案生成。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="eval/dataset.jsonl")
    parser.add_argument(
        "--base-details", default="eval/baidu_highperf_ab_details.json"
    )
    parser.add_argument(
        "--standard-details", default="eval/baidu_standard_ab_details.json"
    )
    parser.add_argument(
        "--standard-retest-details",
        default="eval/baidu_standard_ernie45_retest_20260728_details.json",
    )
    parser.add_argument(
        "--details", default="eval/baidu_multisource_aggregation_details.json"
    )
    parser.add_argument(
        "--report", default="eval/baidu_multisource_aggregation_report.md"
    )
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--metric-k", type=int, default=10)
    parser.add_argument("--rrf-k", type=int, default=60)
    args = parser.parse_args()

    rows = _load_queries(args.dataset, args.queries)
    base = _load_json(Path(args.base_details), {})
    standard = _load_json(Path(args.standard_details), {})
    standard_retest = _load_json(Path(args.standard_retest_details), {})
    if not base.get("queries") or not standard.get("queries"):
        raise ValueError("缺少三接口首轮评测缓存")

    retest_usage = standard_retest.get("summary", {}).get("usage", {})
    retest_successes = int(
        standard_retest.get("summary", {})
        .get("generation", {})
        .get("successes")
        or 0
    )
    model_cost = float(retest_usage.get("estimated_model_cost_rmb") or 0.0)
    standard_model_cost_mean_rmb = (
        model_cost / retest_successes if retest_successes else 0.0
    )

    query_rows: dict[str, dict[str, Any]] = {}
    for item in rows:
        query = item["query"]
        if query not in base["queries"] or query not in standard["queries"]:
            raise ValueError(f"缓存缺少 Query: {query}")
        query_rows[query] = _query_analysis(
            base["queries"][query],
            standard["queries"][query],
            args.metric_k,
            args.rrf_k,
        )
        query_rows[query]["type"] = item.get("type", "")

    summary = _summarize(
        query_rows,
        args.metric_k,
        args.rrf_k,
        standard_model_cost_mean_rmb,
        base.get("summary", {}).get("generated_at_utc"),
    )
    payload = {
        "config": {
            "base_details": args.base_details,
            "standard_details": args.standard_details,
            "standard_retest_details_for_cost": args.standard_retest_details,
            "metric_k": args.metric_k,
            "rrf_k": args.rrf_k,
        },
        "queries": query_rows,
        "summary": summary,
    }
    _save_json(Path(args.details), payload)
    Path(args.report).write_text(_report(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {args.details}")
    print(f"wrote {args.report}")


if __name__ == "__main__":
    main()
