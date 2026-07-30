"""Extend the fixed 10-query reranker evaluation with Qwen3-Reranker-8B."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

from eval.reranker_10_ab import (
    QWEN_INSTRUCTION,
    _deduplicate,
    _document_text,
    _latency,
    _mean_metrics,
    _metrics,
    _ref_key,
    _score_calibration,
)
from eval.reranker_10_qwen4b import _pairwise, _post_qwen_model
from src.config import Settings


MODEL_KEY = "qwen8b"
MODEL_NAME = "Qwen/Qwen3-Reranker-8B"
MODEL_NAMES = {
    MODEL_KEY: MODEL_NAME,
    "qwen4b": "Qwen/Qwen3-Reranker-4B",
    "qwen": "Qwen/Qwen3-Reranker-0.6B",
    "bge": "BAAI/bge-reranker-v2-m3",
}


def _fourway_winner(row: dict) -> str:
    scores = {
        key: row["models"][key]["metrics"]["ndcg_at_10"]
        for key in MODEL_NAMES
    }
    best = max(scores.values())
    winners = [key for key, value in scores.items() if abs(value - best) <= 1e-9]
    return winners[0] if len(winners) == 1 else "tie"


def _report(details: dict) -> str:
    summary = details["summary"]
    lines = [
        "# Qwen3-Reranker-8B vs 4B vs 0.6B vs BGE（真实检索 n=10）",
        "",
        f"- 8B 测试时间：`{details['qwen8b_generated_at_utc']}`",
        f"- 4B 测试时间：`{details['qwen4b_generated_at_utc']}`",
        f"- 0.6B/BGE 测试时间：`{details['baseline_generated_at_utc']}`",
        f"- 候选数据抓取时间：`{details['source_generated_at_utc']}`",
        "- 四款模型使用完全相同的每条最多 20 个真实搜索候选和 0–3 相关性标签",
        f"- Qwen instruction：`{QWEN_INSTRUCTION}`",
        "",
        "## 汇总",
        "",
        "| 模型 | nDCG@10 | Recall@10 | P@5 | MRR | 平均延迟 | P50 | P95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("qwen8b", "qwen4b", "qwen", "bge"):
        metric = summary["metrics"][key]
        latency = summary["latency"][key]
        lines.append(
            f"| `{MODEL_NAMES[key]}` | {metric['ndcg_at_10']:.4f} | "
            f"{metric['recall_at_10']:.4f} | {metric['precision_at_5']:.4f} | "
            f"{metric['mrr']:.4f} | {latency['mean_ms']:.1f} ms | "
            f"{latency['p50_ms']:.1f} ms | {latency['p95_ms']:.1f} ms |"
        )

    lines += ["", "## 配对比较", ""]
    for key, label in (
        ("qwen8b_vs_qwen4b", "8B − 4B"),
        ("qwen8b_vs_qwen", "8B − 0.6B"),
        ("qwen8b_vs_bge", "8B − BGE"),
        ("qwen4b_vs_qwen", "4B − 0.6B"),
    ):
        row = summary["pairwise"][key]
        wins, ties, losses = row["wins_ties_losses"]
        lines.append(
            f"- {label} nDCG@10：`{row['mean_delta']:+.4f}`，"
            f"bootstrap 95% CI `[{row['ci95'][0]:+.4f}, {row['ci95'][1]:+.4f}]`，"
            f"胜/平/负 `{wins}/{ties}/{losses}`"
        )

    qwen8b_p50 = summary["latency"]["qwen8b"]["p50_ms"]
    qwen4b_p50 = summary["latency"]["qwen4b"]["p50_ms"]
    qwen_p50 = summary["latency"]["qwen"]["p50_ms"]
    bge_p50 = summary["latency"]["bge"]["p50_ms"]
    lines += [
        "",
        f"- 8B P50 延迟是 4B 的 `{qwen8b_p50 / qwen4b_p50:.2f}×`，"
        f"是 0.6B 的 `{qwen8b_p50 / qwen_p50:.2f}×`，"
        f"是 BGE 的 `{qwen8b_p50 / bge_p50:.2f}×`。",
        "",
        "## 分数刻度",
        "",
        "| 模型 | 全部文档平均分 | 分数≥0.9 | 分数≥0.99 | rel=1 中位数 | rel=3 中位数 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key in ("qwen8b", "qwen4b", "qwen", "bge"):
        calibration = summary["score_calibration"][key]
        lines.append(
            f"| `{MODEL_NAMES[key]}` | {calibration['score_mean']:.4f} | "
            f"{calibration['fraction_gte_0_9']:.1%} | "
            f"{calibration['fraction_gte_0_99']:.1%} | "
            f"{calibration['by_relevance']['1']['median']:.4f} | "
            f"{calibration['by_relevance']['3']['median']:.4f} |"
        )

    lines += [
        "",
        "## 单 Query",
        "",
        "| Query | 类型 | 8B | 4B | 0.6B | BGE | 最优 | 8B ms |",
        "|---|---|---:|---:|---:|---:|---|---:|",
    ]
    for row in details["queries"]:
        query = row["query"].replace("|", "\\|")
        lines.append(
            f"| {query} | {row['type']} | "
            f"{row['models']['qwen8b']['metrics']['ndcg_at_10']:.4f} | "
            f"{row['models']['qwen4b']['metrics']['ndcg_at_10']:.4f} | "
            f"{row['models']['qwen']['metrics']['ndcg_at_10']:.4f} | "
            f"{row['models']['bge']['metrics']['ndcg_at_10']:.4f} | "
            f"{row['fourway_winner']} | "
            f"{row['models']['qwen8b']['elapsed_ms']:.1f} |"
        )

    lines += [
        "",
        "## 限制",
        "",
        "- n=10 是先导样本；配对置信区间跨 0 时不能认定差异稳定显著。",
        "- 标签来自同一 LLM judge，尚未逐条人工复核。",
        "- 四款模型不是同一时刻调用；延迟只能比较量级与中位数。",
        "- relevance_score 刻度不同，固定阈值需要按模型分别校准。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        default="eval/reranker_10_threeway_details.json",
    )
    parser.add_argument(
        "--source",
        default="eval/baidu_standard_ab_details.json",
    )
    parser.add_argument(
        "--details",
        default="eval/reranker_10_fourway_details.json",
    )
    parser.add_argument(
        "--report",
        default="eval/reranker_10_fourway_report.md",
    )
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.siliconflow_api_key:
        raise ValueError("缺少 SILICONFLOW_API_KEY")

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    source = json.loads(Path(args.source).read_text(encoding="utf-8"))
    details_path = Path(args.details)
    if details_path.exists() and not args.refresh:
        details = json.loads(details_path.read_text(encoding="utf-8"))
    else:
        details = json.loads(json.dumps(baseline))

    if details.get("query_count") != 10:
        raise ValueError("baseline 不是 10-query 结果")

    url = settings.siliconflow_base_url.rstrip("/") + "/rerank"
    session = requests.Session()
    try:
        for index, row in enumerate(details["queries"], 1):
            if MODEL_KEY in row["models"] and not args.refresh:
                print(f"[{index:02d}/10] reuse qwen8b {row['query'][:30]}", flush=True)
                continue

            source_row = source["queries"][row["query"]]
            refs = _deduplicate(source_row["standard"]["references"])
            refs = refs[: details["candidate_limit"]]
            keys = [_ref_key(ref, ref_index) for ref_index, ref in enumerate(refs)]
            labels = [int(source_row["relevance_labels"][key]) for key in keys]
            if labels != row["labels"]:
                raise ValueError(f"{row['query']} 候选或标签与 baseline 不一致")

            result = _post_qwen_model(
                session,
                url=url,
                api_key=settings.siliconflow_api_key,
                model_name=MODEL_NAME,
                query=row["query"],
                documents=[_document_text(ref) for ref in refs],
            )
            metric, ranking = _metrics(result["scores"], labels)
            row["models"][MODEL_KEY] = {
                **result,
                "metrics": metric,
                "ranking": ranking,
                "top5": [
                    {
                        "rank": rank + 1,
                        "candidate_index": candidate_index,
                        "relevance": labels[candidate_index],
                        "score": result["scores"][candidate_index],
                        "title": str(refs[candidate_index].get("title") or ""),
                        "url": str(refs[candidate_index].get("url") or ""),
                    }
                    for rank, candidate_index in enumerate(ranking[:5])
                ],
            }
            details_path.write_text(
                json.dumps(details, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                f"[{index:02d}/10] qwen8b nDCG={metric['ndcg_at_10']:.4f} "
                f"latency={result['elapsed_ms']:.1f}ms {row['query'][:28]}",
                flush=True,
            )
    finally:
        session.close()

    for row in details["queries"]:
        row["fourway_winner"] = _fourway_winner(row)

    metrics = {
        key: _mean_metrics(details["queries"], key) for key in MODEL_NAMES
    }
    measured_times = [
        row["models"]["qwen8b"].get("measured_at_utc")
        for row in details["queries"]
        if row["models"]["qwen8b"].get("measured_at_utc")
    ]
    details["version"] = 3
    details["models"] = MODEL_NAMES
    details["qwen8b_generated_at_utc"] = (
        max(measured_times)
        if measured_times
        else details.get("qwen8b_generated_at_utc")
    )
    details["summary"] = {
        "metrics": metrics,
        "latency": {
            key: _latency(details["queries"], key) for key in MODEL_NAMES
        },
        "score_calibration": {
            key: _score_calibration(details["queries"], key)
            for key in MODEL_NAMES
        },
        "pairwise": {
            "qwen8b_vs_qwen4b": _pairwise(
                details["queries"], "qwen8b", "qwen4b"
            ),
            "qwen8b_vs_qwen": _pairwise(
                details["queries"], "qwen8b", "qwen"
            ),
            "qwen8b_vs_bge": _pairwise(
                details["queries"], "qwen8b", "bge"
            ),
            "qwen4b_vs_qwen": _pairwise(
                details["queries"], "qwen4b", "qwen"
            ),
        },
        "fourway_wins": {
            key: sum(row["fourway_winner"] == key for row in details["queries"])
            for key in (*MODEL_NAMES, "tie")
        },
    }
    details_path.write_text(
        json.dumps(details, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.report).write_text(_report(details), encoding="utf-8")
    print(f"details: {details_path}", flush=True)
    print(f"report:  {args.report}", flush=True)


if __name__ == "__main__":
    main()
