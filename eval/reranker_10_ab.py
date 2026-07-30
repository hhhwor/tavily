"""Live 10-query SiliconFlow reranker A/B evaluation.

The candidates are real Baidu standard-search results captured in
``baidu_standard_ab_details.json``.  Existing 0-3 relevance labels are reused
so this runner only spends reranker requests and compares both models on the
exact same documents.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any, Sequence

import requests

from eval import metrics as M
from src.config import Settings
from src.pipeline.dedup import normalize_url


MODELS = {
    "bge": "BAAI/bge-reranker-v2-m3",
    "qwen": "Qwen/Qwen3-Reranker-0.6B",
}

QWEN_INSTRUCTION = (
    "Given a web search query, rank passages by whether they directly, "
    "correctly, and sufficiently answer the query. For time-sensitive queries, "
    "prefer current information. Ignore keyword-only mentions."
)

DEFAULT_QUERIES = (
    "三星堆遗址在哪个省",
    "光合作用的基本过程是什么",
    "2026年人工智能领域有哪些最新进展",
    "今天A股大盘行情怎么样",
    "Transformer 和 RNN 在长序列建模上的区别",
    "RAG 检索增强生成和模型微调各自的优缺点",
    "向量数据库 HNSW 索引的原理",
    "LangChain 的 agent 是如何调用工具的",
    "如何评估搜索引擎的检索质量",
    "TC3-HMAC-SHA256 签名算法的步骤",
)


def _ref_key(ref: dict[str, Any], index: int = 0) -> str:
    url = str(ref.get("url") or "")
    return normalize_url(url) or url or f"title:{ref.get('title', '')}:{index}"


def _document_text(ref: dict[str, Any]) -> str:
    content = str(ref.get("content") or ref.get("snippet") or "")[:700]
    return (
        f"Title: {str(ref.get('title') or '')[:180]}\n"
        f"Date: {str(ref.get('date') or '')[:40]}\n"
        f"Content: {content}"
    ).strip()


def _deduplicate(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for index, ref in enumerate(refs):
        key = _ref_key(ref, index)
        if key in seen:
            continue
        seen.add(key)
        output.append(ref)
    return output


def _post_rerank(
    session: requests.Session,
    *,
    url: str,
    api_key: str,
    model_key: str,
    query: str,
    documents: Sequence[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": MODELS[model_key],
        "query": query,
        "documents": list(documents),
        "top_n": len(documents),
        "return_documents": False,
    }
    if model_key == "qwen":
        payload["instruction"] = QWEN_INSTRUCTION

    last_error: str | None = None
    for attempt in range(3):
        started = time.perf_counter()
        response = session.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(10, 90),
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if response.ok:
            data = response.json()
            by_index = {
                int(item["index"]): float(item["relevance_score"])
                for item in data.get("results") or []
            }
            expected = set(range(len(documents)))
            if set(by_index) != expected:
                missing = sorted(expected - set(by_index))
                raise RuntimeError(
                    f"{MODELS[model_key]} response missing indices: {missing}"
                )
            return {
                "elapsed_ms": elapsed_ms,
                "scores": [by_index[index] for index in range(len(documents))],
                "meta": data.get("meta") or {},
                "request_id": data.get("id"),
            }

        try:
            body = response.json()
            last_error = f"HTTP {response.status_code}: {body.get('message') or body}"
        except Exception:
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code not in {429, 503, 504}:
            break
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"{MODELS[model_key]} rerank failed: {last_error}")


def _metrics(
    scores: Sequence[float],
    labels: Sequence[int],
) -> tuple[dict[str, float], list[int]]:
    ranking = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
    ranked_rels = [labels[index] for index in ranking]
    return (
        {
            "ndcg_at_10": M.ndcg_at_k(ranked_rels, list(labels), 10),
            "recall_at_10": M.recall_at_k(ranked_rels, list(labels), 10),
            "precision_at_5": M.precision_at_k(ranked_rels, 5),
            "mrr": M.mrr(ranked_rels),
        },
        ranking,
    )


def _mean_metrics(rows: Sequence[dict[str, Any]], model_key: str) -> dict[str, float]:
    keys = ("ndcg_at_10", "recall_at_10", "precision_at_5", "mrr")
    return {
        key: statistics.fmean(row["models"][model_key]["metrics"][key] for row in rows)
        for key in keys
    }


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _latency(rows: Sequence[dict[str, Any]], model_key: str) -> dict[str, float]:
    values = [float(row["models"][model_key]["elapsed_ms"]) for row in rows]
    return {
        "mean_ms": statistics.fmean(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
    }


def _score_calibration(
    rows: Sequence[dict[str, Any]], model_key: str
) -> dict[str, Any]:
    scores: list[float] = []
    by_relevance: dict[int, list[float]] = {label: [] for label in range(4)}
    for row in rows:
        model_scores = row["models"][model_key]["scores"]
        scores.extend(model_scores)
        for score, relevance in zip(model_scores, row["labels"]):
            by_relevance[int(relevance)].append(float(score))
    return {
        "score_mean": statistics.fmean(scores),
        "fraction_gte_0_9": sum(score >= 0.9 for score in scores) / len(scores),
        "fraction_gte_0_99": sum(score >= 0.99 for score in scores) / len(scores),
        "by_relevance": {
            str(relevance): {
                "count": len(values),
                "mean": statistics.fmean(values) if values else None,
                "median": statistics.median(values) if values else None,
            }
            for relevance, values in by_relevance.items()
        },
    }


def _paired_bootstrap(
    differences: Sequence[float], seed: int = 20260723
) -> dict[str, Any]:
    rng = random.Random(seed)
    size = len(differences)
    samples = sorted(
        statistics.fmean(rng.choice(differences) for _ in range(size))
        for _ in range(20_000)
    )
    return {
        "mean_delta": statistics.fmean(differences),
        "ci95": [samples[500], samples[19_499]],
    }


def _winner(left: float, right: float, tolerance: float = 1e-9) -> str:
    delta = left - right
    if abs(delta) <= tolerance:
        return "tie"
    return "qwen" if delta > 0 else "bge"


def _report(details: dict[str, Any]) -> str:
    summary = details["summary"]
    lines = [
        "# Qwen3-Reranker-0.6B vs BGE-Reranker-v2-m3（真实检索 n=10）",
        "",
        f"- 测试时间：`{details['generated_at_utc']}`",
        f"- 候选数据：百度标准搜索真实结果，原始抓取时间 `{details['source_generated_at_utc']}`",
        "- 每条 Query 使用同一批最多 20 个候选；相关性标签为既有 0–3 分盲评标签",
        f"- Qwen instruction：`{QWEN_INSTRUCTION}`",
        "",
        "## 汇总",
        "",
        "| 模型 | nDCG@10 | Recall@10 | P@5 | MRR | 平均延迟 | P50 | P95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("qwen", "bge"):
        metric = summary["metrics"][key]
        latency = summary["latency"][key]
        lines.append(
            f"| `{MODELS[key]}` | {metric['ndcg_at_10']:.4f} | "
            f"{metric['recall_at_10']:.4f} | {metric['precision_at_5']:.4f} | "
            f"{metric['mrr']:.4f} | {latency['mean_ms']:.1f} ms | "
            f"{latency['p50_ms']:.1f} ms | {latency['p95_ms']:.1f} ms |"
        )
    lines += [
        "",
        f"- nDCG 胜/平/负（Qwen 视角）："
        f"`{summary['wins']['qwen']}/{summary['wins']['tie']}/{summary['wins']['bge']}`",
        f"- Qwen − BGE 平均 nDCG@10："
        f"`{summary['metric_delta']['ndcg_at_10']:+.4f}`",
        f"- Query 级配对 bootstrap 95% CI："
        f"`[{summary['ndcg_bootstrap']['ci95'][0]:+.4f}, "
        f"{summary['ndcg_bootstrap']['ci95'][1]:+.4f}]`（区间跨 0）",
        "",
        "## 分数刻度",
        "",
        "| 模型 | 全部文档平均分 | 分数≥0.9 | 分数≥0.99 | rel=1 中位数 | rel=3 中位数 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key in ("qwen", "bge"):
        calibration = summary["score_calibration"][key]
        lines.append(
            f"| `{MODELS[key]}` | {calibration['score_mean']:.4f} | "
            f"{calibration['fraction_gte_0_9']:.1%} | "
            f"{calibration['fraction_gte_0_99']:.1%} | "
            f"{calibration['by_relevance']['1']['median']:.4f} | "
            f"{calibration['by_relevance']['3']['median']:.4f} |"
        )
    lines += [
        "",
        "Qwen 分数明显更饱和；切换模型时不能沿用 BGE 的固定阈值，需要按业务标签重新校准。",
        "",
        "## 单 Query",
        "",
        "| Query | 类型 | 候选 | Qwen nDCG | BGE nDCG | 胜者 | Qwen ms | BGE ms |",
        "|---|---|---:|---:|---:|---|---:|---:|",
    ]
    for row in details["queries"]:
        query = row["query"].replace("|", "\\|")
        qwen = row["models"]["qwen"]
        bge = row["models"]["bge"]
        lines.append(
            f"| {query} | {row['type']} | {row['candidate_count']} | "
            f"{qwen['metrics']['ndcg_at_10']:.4f} | "
            f"{bge['metrics']['ndcg_at_10']:.4f} | {row['ndcg_winner']} | "
            f"{qwen['elapsed_ms']:.1f} | {bge['elapsed_ms']:.1f} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- 这是小样本先导测试，适合发现明显趋势，不足以给出统计显著结论。",
        "- 标签来自同一 LLM judge，尚未逐条人工复核；两款模型共享该误差来源。",
        "- 延迟包含公网传输和 SiliconFlow 排队时间，不等于模型纯推理时间。",
        "- 两款模型分数刻度不同；质量比较基于排序位置，不比较绝对 relevance_score。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        default="eval/baidu_standard_ab_details.json",
    )
    parser.add_argument(
        "--details",
        default="eval/reranker_10_ab_details.json",
    )
    parser.add_argument(
        "--report",
        default="eval/reranker_10_ab_report.md",
    )
    parser.add_argument("--candidate-count", type=int, default=20)
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.siliconflow_api_key:
        raise ValueError("缺少 SILICONFLOW_API_KEY")

    source = json.loads(Path(args.source).read_text(encoding="utf-8"))
    source_queries = source.get("queries") or {}
    missing = [query for query in DEFAULT_QUERIES if query not in source_queries]
    if missing:
        raise ValueError(f"源数据缺少 Query: {missing}")

    url = settings.siliconflow_base_url.rstrip("/") + "/rerank"
    output_rows: list[dict[str, Any]] = []
    session = requests.Session()
    try:
        for query_index, query in enumerate(DEFAULT_QUERIES):
            source_row = source_queries[query]
            refs = _deduplicate(source_row["standard"]["references"])
            refs = refs[: args.candidate_count]
            labels_by_key = source_row["relevance_labels"]
            keys = [_ref_key(ref, index) for index, ref in enumerate(refs)]
            missing_labels = [key for key in keys if key not in labels_by_key]
            if missing_labels:
                raise ValueError(f"{query} 缺少相关性标签: {missing_labels}")

            documents = [_document_text(ref) for ref in refs]
            labels = [int(labels_by_key[key]) for key in keys]
            model_results: dict[str, Any] = {}

            # Alternate request order to reduce systematic server-load bias.
            order = ("bge", "qwen") if query_index % 2 == 0 else ("qwen", "bge")
            for model_key in order:
                result = _post_rerank(
                    session,
                    url=url,
                    api_key=settings.siliconflow_api_key,
                    model_key=model_key,
                    query=query,
                    documents=documents,
                )
                metric, ranking = _metrics(result["scores"], labels)
                model_results[model_key] = {
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
                print(
                    f"[{query_index + 1:02d}/10] {model_key:4s} "
                    f"nDCG={metric['ndcg_at_10']:.4f} "
                    f"latency={result['elapsed_ms']:.1f}ms {query[:28]}",
                    flush=True,
                )

            winner = _winner(
                model_results["qwen"]["metrics"]["ndcg_at_10"],
                model_results["bge"]["metrics"]["ndcg_at_10"],
            )
            output_rows.append(
                {
                    "query": query,
                    "type": source_row.get("type") or "",
                    "candidate_count": len(refs),
                    "label_distribution": {
                        str(label): labels.count(label) for label in range(4)
                    },
                    "labels": labels,
                    "ndcg_winner": winner,
                    "models": model_results,
                }
            )
    finally:
        session.close()

    mean_metrics = {
        model_key: _mean_metrics(output_rows, model_key) for model_key in MODELS
    }
    ndcg_differences = [
        row["models"]["qwen"]["metrics"]["ndcg_at_10"]
        - row["models"]["bge"]["metrics"]["ndcg_at_10"]
        for row in output_rows
    ]
    wins = {
        key: sum(row["ndcg_winner"] == key for row in output_rows)
        for key in ("qwen", "tie", "bge")
    }
    details = {
        "version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": args.source,
        "source_generated_at_utc": source.get("summary", {}).get("generated_at_utc"),
        "models": MODELS,
        "qwen_instruction": QWEN_INSTRUCTION,
        "candidate_limit": args.candidate_count,
        "query_count": len(output_rows),
        "summary": {
            "metrics": mean_metrics,
            "metric_delta": {
                key: mean_metrics["qwen"][key] - mean_metrics["bge"][key]
                for key in mean_metrics["qwen"]
            },
            "ndcg_bootstrap": _paired_bootstrap(ndcg_differences),
            "latency": {
                model_key: _latency(output_rows, model_key) for model_key in MODELS
            },
            "score_calibration": {
                model_key: _score_calibration(output_rows, model_key)
                for model_key in MODELS
            },
            "wins": wins,
        },
        "queries": output_rows,
    }
    details_path = Path(args.details)
    report_path = Path(args.report)
    details_path.write_text(
        json.dumps(details, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(_report(details), encoding="utf-8")
    print(f"details: {details_path}", flush=True)
    print(f"report:  {report_path}", flush=True)


if __name__ == "__main__":
    main()
