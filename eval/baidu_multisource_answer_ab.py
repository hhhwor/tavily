"""Answer-quality A/B for the three-Baidu-source aggregation pipeline.

This runner reuses the same-window retrieval cache from 2026-07-23:

  aggregate_sf:
      web_search + web_summary + chat/completions references
      -> URL dedup -> SiliconFlow scores -> fixed Claude evidence answerer

It compares that deployable aggregate answer against:
  - the native high-performance answer;
  - standard references + SiliconFlow + the same fixed Claude answerer.

Each generated answer and judge result is checkpointed.  Retrieval and
SiliconFlow are not called again.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from eval.baidu_highperf_ab import (
    ClaudeClient,
    _baseline_answer,
    _dedup_refs,
    _load_json,
    _load_queries,
    _paired_bootstrap,
    _rank_refs,
    _read_project_env,
    _save_json,
)
from eval.baidu_standard_ab import _judge_three_answers


_VERSION = 1
_DIMS = ("score", "correctness", "completeness", "grounding", "freshness")
_SYSTEMS = ("aggregate_sf", "highperf", "standard_sf")


def _summarize(
    details: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    judged = [details["queries"][item["query"]] for item in rows]
    quality: dict[str, Any] = {
        system: {
            dim: statistics.fmean(row["judge"][system][dim] for row in judged)
            for dim in _DIMS
        }
        for system in _SYSTEMS
    }
    wins = {system: 0 for system in (*_SYSTEMS, "tie")}
    for row in judged:
        wins[row["judge"]["winner"]] += 1
    quality["wins"] = wins

    checks = {}
    for other, seed in (("highperf", 20260820), ("standard_sf", 20260821)):
        for dim, offset in (("score", 0), ("completeness", 1), ("grounding", 2)):
            differences = [
                row["judge"]["aggregate_sf"][dim] - row["judge"][other][dim]
                for row in judged
            ]
            checks[f"aggregate_sf_minus_{other}_{dim}"] = _paired_bootstrap(
                differences, seed + offset
            )

    return {
        "generated_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "source_snapshot_utc": details["config"]["source_snapshot_utc"],
        "query_count": len(judged),
        "judge_model": details["config"]["judge_model"],
        "quality": quality,
        "paired_checks": checks,
        "answer_chars": {
            system: {
                "mean": statistics.fmean(
                    len(row["answers"][system]) for row in judged
                ),
                "min": min(len(row["answers"][system]) for row in judged),
                "max": max(len(row["answers"][system]) for row in judged),
            }
            for system in _SYSTEMS
        },
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _report(summary: dict[str, Any]) -> str:
    quality = summary["quality"]
    lines = [
        "# 百度三源聚合答案质量 A/B",
        "",
        f"- 检索快照：`{summary['source_snapshot_utc']}`",
        f"- Query：`{summary['query_count']}`",
        f"- 固定回答器/评审模型：`{summary['judge_model']}`",
        "- 聚合管线：三接口 Top-20 → URL 去重 → SiliconFlow → Top-8 证据 → 固定回答器",
        "- 高性能版使用百度原生答案；标准版使用标准候选 + SF + 同一固定回答器。",
        "",
        "| 配置 | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "aggregate_sf": "三源聚合 + SF + 固定回答器",
        "highperf": "高性能版原生答案",
        "standard_sf": "标准版 + SF + 固定回答器",
    }
    for system in _SYSTEMS:
        row = quality[system]
        lines.append(
            f"| {labels[system]} | {_fmt(row['score'])} | "
            f"{_fmt(row['correctness'])} | {_fmt(row['completeness'])} | "
            f"{_fmt(row['grounding'])} | {_fmt(row['freshness'])} |"
        )
    lines += [
        "",
        f"- 胜负：`{quality['wins']}`",
        "",
        "## 聚合方案配对差异",
        "",
        "| 对照 | Δ总分（95% CI） | 胜/平/负 | ΔCompleteness | ΔGrounding |",
        "|---|---:|---:|---:|---:|",
    ]
    checks = summary["paired_checks"]
    for other in ("highperf", "standard_sf"):
        score = checks[f"aggregate_sf_minus_{other}_score"]
        completeness = checks[f"aggregate_sf_minus_{other}_completeness"]
        grounding = checks[f"aggregate_sf_minus_{other}_grounding"]
        lines.append(
            f"| 聚合 − {labels[other]} | {_fmt(score['mean_delta'])} "
            f"`[{_fmt(score['ci95'][0])}, {_fmt(score['ci95'][1])}]` | "
            f"`{score['wins_ties_losses']}` | "
            f"{_fmt(completeness['mean_delta'])} | "
            f"{_fmt(grounding['mean_delta'])} |"
        )
    lines += [
        "",
        "## 限制",
        "",
        "- 固定回答器与评审使用同一 Claude 模型，存在自偏好风险。",
        "- 高性能版使用原生答案，另外两路使用固定回答器，不是纯模型控制实验。",
        "- 仅使用重排后的前 8 条证据；聚合候选覆盖提升不保证进入答案上下文。",
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
        "--details", default="eval/baidu_multisource_answer_ab_details.json"
    )
    parser.add_argument(
        "--report", default="eval/baidu_multisource_answer_ab_report.md"
    )
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    args = parser.parse_args()

    rows = _load_queries(args.dataset, args.queries)
    base = _load_json(Path(args.base_details), {})
    standard = _load_json(Path(args.standard_details), {})
    if not base.get("queries") or not standard.get("queries"):
        raise ValueError("缺少三接口首轮评测缓存")

    env = _read_project_env()
    if not env.get("ANTHROPIC_API_KEY"):
        raise ValueError("缺少 ANTHROPIC_API_KEY")
    client = ClaudeClient(
        env["ANTHROPIC_API_KEY"],
        env.get("ANTHROPIC_BASE_URL", "").strip(),
        args.judge_model,
    )

    details_path = Path(args.details)
    details = _load_json(
        details_path,
        {
            "version": _VERSION,
            "config": {
                "source_snapshot_utc": (
                    base.get("summary", {}).get("generated_at_utc")
                ),
                "judge_model": args.judge_model,
                "evidence_limit": 8,
                "ranking": "siliconflow",
            },
            "queries": {},
        },
    )
    if details.get("version") != _VERSION:
        raise ValueError("缓存版本不匹配")
    if details.get("config", {}).get("judge_model") != args.judge_model:
        raise ValueError("缓存模型与 --judge-model 不一致")

    for index, item in enumerate(rows, 1):
        query = item["query"]
        base_query = base["queries"][query]
        standard_query = standard["queries"][query]
        cached = details["queries"].setdefault(
            query, {"type": item.get("type", "")}
        )
        pool = _dedup_refs(
            (
                base_query["current"]["references"],
                base_query["high_full"]["references"],
                standard_query["standard"]["references"],
            )
        )
        scores = standard_query["siliconflow_scores"]
        aggregate_refs = _rank_refs(pool, scores)
        high_refs = _rank_refs(
            base_query["high_full"]["references"], None, baidu=True
        )
        standard_refs = _rank_refs(
            standard_query["standard"]["references"], scores
        )

        if not cached.get("aggregate_answer"):
            cached["aggregate_answer"] = _baseline_answer(
                client, query, aggregate_refs
            )
            _save_json(details_path, details)

        answers = {
            "aggregate_sf": cached["aggregate_answer"],
            "highperf": base_query["high_full"].get("answer") or "",
            "standard_sf": standard_query.get("standard_answer") or "",
        }
        if not all(answers.values()):
            missing = [name for name, answer in answers.items() if not answer]
            raise ValueError(f"{query} 缺少答案：{missing}")

        if not cached.get("judge"):
            cached["judge"] = _judge_three_answers(
                client,
                query,
                {
                    "aggregate_sf": {
                        "answer": answers["aggregate_sf"],
                        "references": aggregate_refs,
                    },
                    "highperf": {
                        "answer": answers["highperf"],
                        "references": high_refs,
                    },
                    "standard_sf": {
                        "answer": answers["standard_sf"],
                        "references": standard_refs,
                    },
                },
            )
            _save_json(details_path, details)

        cached["answers"] = answers
        cached["reference_counts"] = {
            "aggregate_sf": len(aggregate_refs),
            "highperf": len(high_refs),
            "standard_sf": len(standard_refs),
        }
        _save_json(details_path, details)
        print(
            f"[{index:02d}/{len(rows)}] {query[:30]} "
            f"aggregate={len(answers['aggregate_sf'])}chars "
            f"winner={cached['judge']['winner']}",
            flush=True,
        )

    summary = _summarize(details, rows)
    details["summary"] = summary
    _save_json(details_path, details)
    Path(args.report).write_text(_report(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"wrote {args.details}", flush=True)
    print(f"wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
