"""End-to-end RAG answer evaluation for the fixed four-reranker dataset.

For every query and reranker, this runner:

1. reconstructs the model's Top-5 evidence from the fixed candidate pool;
2. asks the same answer model to answer using only that evidence;
3. audits citation indices programmatically;
4. presents all four answers to one position-shuffled blind judge.

Every network result is checkpointed so reruns reuse completed work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

from eval.baidu_highperf_ab import ClaudeClient, _ask_json, _evidence
from eval.reranker_10_ab import _deduplicate
from eval.reranker_10_qwen8b import MODEL_NAMES


PROMPT_VERSION = 1

ANSWER_PROMPT = """你是检索增强问答助手。请严格只使用给出的 evidence 回答 query。

要求：
- 使用中文，直接回答核心问题，完整但紧凑；
- 每个关键事实都用 [1]、[2] 形式引用其证据；
- 引用编号只能来自 evidence.id；
- 对时效查询优先采用日期最新且能直接回答的证据；
- 多篇证据冲突时明确指出，不自行猜测；
- 证据不足时明确说明缺口，不得补充外部事实；
- 不要输出“根据提供的材料”等无信息量开场白。"""

JUDGE_PROMPT = """你是 RAG 最终答案质量评审。给定同一 query 的四个匿名答案及各自实际证据。
只根据给出的答案和证据评分，不得使用外部知识。

对 A、B、C、D 分别评价：
- correctness：关键结论是否被证据支持；
- completeness：是否覆盖查询核心意图；
- grounding：引用是否存在且真正支持相邻陈述；
- freshness：时效问题是否使用最新有效证据；非时效问题正常评价；
- score：总体可用性。

避免位置偏好，不因答案更长而自动加分。若证据本身不足，诚实说明缺口应优于编造。
support_audit.unsupported_refs 非空时必须降低 grounding。

只输出合法 JSON，不要输出其他文字：
{"A":{"score":0,"correctness":0,"completeness":0,"grounding":0,"freshness":0},
 "B":{"score":0,"correctness":0,"completeness":0,"grounding":0,"freshness":0},
 "C":{"score":0,"correctness":0,"completeness":0,"grounding":0,"freshness":0},
 "D":{"score":0,"correctness":0,"completeness":0,"grounding":0,"freshness":0},
 "winner":"A|B|C|D|tie"}

score 范围 0-10；四个分项范围 0-2。"""

MODEL_ORDER = ("qwen8b", "qwen4b", "qwen", "bge")
POSITIONS = ("A", "B", "C", "D")


def _save(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _answer_once(
    client: ClaudeClient,
    query: str,
    evidence: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    started = time.perf_counter()
    answer = client.ask(
        ANSWER_PROMPT,
        {"query": query, "evidence": list(evidence)},
        900,
    )
    return {
        "text": answer,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "generated_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
    }


def _citation_audit(answer: str, evidence_count: int) -> dict[str, Any]:
    citations = [int(value) for value in re.findall(r"\[(\d+)\]", answer)]
    unsupported = sorted(
        {citation for citation in citations if citation < 1 or citation > evidence_count}
    )
    supported = [
        citation for citation in citations if 1 <= citation <= evidence_count
    ]
    return {
        "citation_count": len(citations),
        "unique_supported_refs": sorted(set(supported)),
        "unsupported_refs": unsupported,
        "has_citations": bool(citations),
        "answer_chars": len(answer),
    }


def _positions(query: str) -> dict[str, str]:
    ordered = sorted(
        MODEL_ORDER,
        key=lambda key: hashlib.sha1(f"{query}\0{key}".encode()).hexdigest(),
    )
    return dict(zip(POSITIONS, ordered))


def _clamp(value: Any, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = 0
    return max(0, min(maximum, number))


def _judge_once(
    client: ClaudeClient,
    query: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    positions = _positions(query)
    payload: dict[str, Any] = {"query": query}
    for position, model_key in positions.items():
        payload[position] = {
            "answer": row["answers"][model_key]["text"],
            "evidence": row["evidence"][model_key],
            "support_audit": row["audits"][model_key],
        }

    started = time.perf_counter()
    raw = _ask_json(client, JUDGE_PROMPT, payload, 1000)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    result: dict[str, Any] = {
        "positions": positions,
        "elapsed_ms": elapsed_ms,
        "judged_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
    }
    for position, model_key in positions.items():
        score = raw.get(position) or {}
        result[model_key] = {
            "score": _clamp(score.get("score"), 10),
            "correctness": _clamp(score.get("correctness"), 2),
            "completeness": _clamp(score.get("completeness"), 2),
            "grounding": _clamp(score.get("grounding"), 2),
            "freshness": _clamp(score.get("freshness"), 2),
        }
    winner_position = str(raw.get("winner") or "tie")
    result["winner"] = positions.get(winner_position, "tie")
    return result


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(details: dict[str, Any]) -> dict[str, Any]:
    rows = list(details["queries"].values())
    dimensions = (
        "score",
        "correctness",
        "completeness",
        "grounding",
        "freshness",
    )
    quality = {
        model_key: {
            dimension: statistics.fmean(
                row["judge"][model_key][dimension] for row in rows
            )
            for dimension in dimensions
        }
        for model_key in MODEL_ORDER
    }
    latency: dict[str, Any] = {}
    citation: dict[str, Any] = {}
    for model_key in MODEL_ORDER:
        answer_ms = [
            float(row["answers"][model_key]["elapsed_ms"]) for row in rows
        ]
        end_to_end_ms = [
            float(row["reranker_latency_ms"][model_key])
            + float(row["answers"][model_key]["elapsed_ms"])
            for row in rows
        ]
        audits = [row["audits"][model_key] for row in rows]
        latency[model_key] = {
            "answer_mean_ms": statistics.fmean(answer_ms),
            "answer_p50_ms": _percentile(answer_ms, 0.50),
            "answer_p95_ms": _percentile(answer_ms, 0.95),
            "rerank_plus_answer_mean_ms": statistics.fmean(end_to_end_ms),
            "rerank_plus_answer_p50_ms": _percentile(end_to_end_ms, 0.50),
            "rerank_plus_answer_p95_ms": _percentile(end_to_end_ms, 0.95),
        }
        citation[model_key] = {
            "answers_with_citations": sum(audit["has_citations"] for audit in audits),
            "unsupported_ref_count": sum(
                len(audit["unsupported_refs"]) for audit in audits
            ),
            "mean_unique_refs": statistics.fmean(
                len(audit["unique_supported_refs"]) for audit in audits
            ),
            "mean_answer_chars": statistics.fmean(
                audit["answer_chars"] for audit in audits
            ),
        }
    evidence_overlap: dict[str, Any] = {}
    for left_index, left in enumerate(MODEL_ORDER):
        for right in MODEL_ORDER[left_index + 1:]:
            jaccards = []
            exact_sets = 0
            for row in rows:
                left_urls = {
                    item["url"] for item in row["evidence"][left]
                }
                right_urls = {
                    item["url"] for item in row["evidence"][right]
                }
                union = left_urls | right_urls
                jaccards.append(
                    len(left_urls & right_urls) / len(union) if union else 1.0
                )
                exact_sets += left_urls == right_urls
            evidence_overlap[f"{left}_vs_{right}"] = {
                "mean_jaccard": statistics.fmean(jaccards),
                "exact_top5_sets": exact_sets,
            }
    return {
        "quality": quality,
        "wins": {
            key: sum(row["judge"]["winner"] == key for row in rows)
            for key in (*MODEL_ORDER, "tie")
        },
        "latency": latency,
        "citation": citation,
        "evidence_overlap": evidence_overlap,
        "judge_position_counts": {
            model_key: {
                position: sum(
                    row["judge"]["positions"].get(position) == model_key
                    for row in rows
                )
                for position in POSITIONS
            }
            for model_key in MODEL_ORDER
        },
    }


def _report(details: dict[str, Any]) -> str:
    summary = details["summary"]
    lines = [
        "# 四款 Reranker 端到端 RAG 答案测试（n=10，Top-5）",
        "",
        f"- generated_at_utc: `{details['generated_at_utc']}`",
        f"- answer/judge model: `{details['config']['answer_model']}`",
        "- 回答器、提示词、候选池和证据数量完全一致；只改变 Reranker 的 Top-5 及顺序",
        "- 四答案对 Judge 匿名，位置按 Query 确定性打乱",
        "",
        "## 答案质量",
        "",
        "| Reranker | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 | 胜次 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model_key in MODEL_ORDER:
        row = summary["quality"][model_key]
        lines.append(
            f"| `{MODEL_NAMES[model_key]}` | {row['score']:.3f} | "
            f"{row['correctness']:.3f} | {row['completeness']:.3f} | "
            f"{row['grounding']:.3f} | {row['freshness']:.3f} | "
            f"{summary['wins'][model_key]} |"
        )
    lines.append(f"| `tie` | - | - | - | - | - | {summary['wins']['tie']} |")

    lines += [
        "",
        "## Top-5 证据集合重合度",
        "",
        "| 模型对 | 平均 Jaccard | 完全相同 Query 数 |",
        "|---|---:|---:|",
    ]
    for key, row in summary["evidence_overlap"].items():
        left, right = key.split("_vs_")
        lines.append(
            f"| `{left}` vs `{right}` | {row['mean_jaccard']:.3f} | "
            f"{row['exact_top5_sets']}/10 |"
        )

    lines += [
        "",
        "## 引用审计",
        "",
        "| Reranker | 有引用答案 | 越界引用 | 平均使用证据数 | 平均答案字符 |",
        "|---|---:|---:|---:|---:|",
    ]
    for model_key in MODEL_ORDER:
        row = summary["citation"][model_key]
        lines.append(
            f"| `{MODEL_NAMES[model_key]}` | "
            f"{row['answers_with_citations']}/10 | "
            f"{row['unsupported_ref_count']} | "
            f"{row['mean_unique_refs']:.2f} | {row['mean_answer_chars']:.1f} |"
        )

    lines += [
        "",
        "## 延迟",
        "",
        "| Reranker | 回答 P50 | 回答 P95 | Rerank+回答 P50 | Rerank+回答 P95 |",
        "|---|---:|---:|---:|---:|",
    ]
    for model_key in MODEL_ORDER:
        row = summary["latency"][model_key]
        lines.append(
            f"| `{MODEL_NAMES[model_key]}` | {row['answer_p50_ms']:.1f} ms | "
            f"{row['answer_p95_ms']:.1f} ms | "
            f"{row['rerank_plus_answer_p50_ms']:.1f} ms | "
            f"{row['rerank_plus_answer_p95_ms']:.1f} ms |"
        )

    lines += [
        "",
        "## 单 Query",
        "",
        "| Query | 类型 | 8B | 4B | 0.6B | BGE | 胜者 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for query, row in details["queries"].items():
        escaped = query.replace("|", "\\|")
        judge = row["judge"]
        lines.append(
            f"| {escaped} | {row['type']} | {judge['qwen8b']['score']} | "
            f"{judge['qwen4b']['score']} | {judge['qwen']['score']} | "
            f"{judge['bge']['score']} | {judge['winner']} |"
        )

    lines += [
        "",
        "## 限制",
        "",
        "- 只有10条中文 Web Query，不能代表英文、论文、专利或企业知识库。",
        "- 相关性标签和答案评分均由模型完成，尚未人工复核。",
        "- 回答器与 Judge 使用同一模型，可能存在共享偏好；匿名与位置轮换只能缓解部分偏差。",
        "- 回答请求并发执行，延迟包含代理、排队与公网波动；端到端延迟为不同阶段测量值之和。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rankings",
        default="eval/reranker_10_fourway_details.json",
    )
    parser.add_argument(
        "--source",
        default="eval/baidu_standard_ab_details.json",
    )
    parser.add_argument(
        "--details",
        default="eval/reranker_10_rag_e2e_details.json",
    )
    parser.add_argument(
        "--report",
        default="eval/reranker_10_rag_e2e_report.md",
    )
    parser.add_argument(
        "--answer-model",
        default="claude-haiku-4-5-20251001",
    )
    parser.add_argument(
        "--judge-model",
        default="claude-haiku-4-5-20251001",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    from eval.baidu_highperf_ab import _read_project_env

    env = _read_project_env()
    if not env.get("ANTHROPIC_API_KEY"):
        raise ValueError("缺少 ANTHROPIC_API_KEY")
    if args.top_k != 5:
        raise ValueError("当前固定排序产物按 Top-5 设计")

    ranking_data = json.loads(Path(args.rankings).read_text(encoding="utf-8"))
    source_data = json.loads(Path(args.source).read_text(encoding="utf-8"))
    details_path = Path(args.details)
    expected_config = {
        "prompt_version": PROMPT_VERSION,
        "rankings": args.rankings,
        "source": args.source,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "top_k": args.top_k,
    }
    if details_path.exists():
        details = json.loads(details_path.read_text(encoding="utf-8"))
        if details.get("config") != expected_config:
            raise ValueError("已有 details 配置不同，请改用新的输出路径")
    else:
        details = {
            "version": 1,
            "config": expected_config,
            "queries": {},
        }

    for ranking_row in ranking_data["queries"]:
        query = ranking_row["query"]
        source_row = source_data["queries"][query]
        refs = _deduplicate(source_row["standard"]["references"])
        refs = refs[: ranking_data["candidate_limit"]]
        row = details["queries"].setdefault(
            query,
            {
                "type": ranking_row["type"],
                "evidence": {},
                "reranker_latency_ms": {},
                "answers": {},
                "audits": {},
            },
        )
        for model_key in MODEL_ORDER:
            ranking = ranking_row["models"][model_key]["ranking"][: args.top_k]
            ranked_refs = [refs[index] for index in ranking]
            row["evidence"][model_key] = _evidence(ranked_refs, limit=args.top_k)
            row["reranker_latency_ms"][model_key] = ranking_row["models"][
                model_key
            ]["elapsed_ms"]
    _save(details_path, details)

    answer_client = ClaudeClient(
        env["ANTHROPIC_API_KEY"],
        env.get("ANTHROPIC_BASE_URL", "").strip(),
        args.answer_model,
    )
    answer_jobs = []
    for query, row in details["queries"].items():
        for model_key in MODEL_ORDER:
            if model_key not in row["answers"]:
                answer_jobs.append((query, model_key, row["evidence"][model_key]))

    if answer_jobs:
        print(f"[answers] generating {len(answer_jobs)} answers", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_answer_once, answer_client, query, evidence):
                (query, model_key)
                for query, model_key, evidence in answer_jobs
            }
            completed = 0
            for future in as_completed(futures):
                query, model_key = futures[future]
                answer = future.result()
                row = details["queries"][query]
                row["answers"][model_key] = answer
                row["audits"][model_key] = _citation_audit(
                    answer["text"], len(row["evidence"][model_key])
                )
                completed += 1
                print(
                    f"[answer {completed:02d}/{len(answer_jobs)}] {model_key:7s} "
                    f"{answer['elapsed_ms']:.1f}ms {query[:26]}",
                    flush=True,
                )
                _save(details_path, details)
    else:
        print("[answers] reuse all cached answers", flush=True)

    judge_client = ClaudeClient(
        env["ANTHROPIC_API_KEY"],
        env.get("ANTHROPIC_BASE_URL", "").strip(),
        args.judge_model,
    )
    judge_jobs = [
        (query, row)
        for query, row in details["queries"].items()
        if "judge" not in row
    ]
    if judge_jobs:
        print(f"[judge] scoring {len(judge_jobs)} four-way comparisons", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_judge_once, judge_client, query, row): query
                for query, row in judge_jobs
            }
            completed = 0
            for future in as_completed(futures):
                query = futures[future]
                judgment = future.result()
                details["queries"][query]["judge"] = judgment
                completed += 1
                print(
                    f"[judge {completed:02d}/{len(judge_jobs)}] "
                    f"winner={judgment['winner']:7s} "
                    f"{judgment['elapsed_ms']:.1f}ms {query[:26]}",
                    flush=True,
                )
                _save(details_path, details)
    else:
        print("[judge] reuse all cached judgments", flush=True)

    completed_times = [
        row["judge"].get("judged_at_utc")
        for row in details["queries"].values()
        if row.get("judge", {}).get("judged_at_utc")
    ]
    details["generated_at_utc"] = (
        max(completed_times)
        if completed_times
        else details.get("generated_at_utc")
    )
    details["summary"] = _summary(details)
    _save(details_path, details)
    Path(args.report).write_text(_report(details), encoding="utf-8")
    print(f"details: {details_path}", flush=True)
    print(f"report:  {args.report}", flush=True)


if __name__ == "__main__":
    main()
