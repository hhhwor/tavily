"""Compare full agent search against baidu-only on vertical R&D scenarios.

The benchmark is from the perspective of an LLM agent that must write a
technology intelligence brief. Good evidence should include academic papers,
patents/applicants, and web/industry signals.

Usage:
  .venv/bin/python -m eval.run_agent_scenario_compare --max-scenarios 2 --no-answer
  .venv/bin/python -m eval.run_agent_scenario_compare
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Dict, List, Optional, Tuple

from eval.agent_answer_eval import AnswerPairJudge, EvidenceAnswerAgent, answer_support_audit
from eval.e2e_judge import ScenarioPairJudge, compact_response
from src.config import settings
from src.engine import SearchEngine
from src.l0 import detect_recency
from src.models import SearchResponse
from src.providers.baidu import BaiduSearchProvider

_REPORT_PATH = "eval/agent_scenario_report.md"
_DETAILS_PATH = "eval/agent_scenario_details.json"


def load_scenarios(path: str, limit: int) -> List[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


def run_full_agent(engine: SearchEngine, task: str, k: int, force_vertical: bool) -> SearchResponse:
    return engine.search(
        task,
        top_k=k,
        include_academic=True if force_vertical else None,
        include_patent=True if force_vertical else None,
    )


def run_baidu_only(provider: BaiduSearchProvider, task: str, k: int) -> SearchResponse:
    t0 = time.time()
    recency = detect_recency(task)
    results = provider.search(task, k, recency)
    return SearchResponse(
        query=task,
        normalized_query=task,
        recency=recency,
        time_sensitive=recency is not None,
        results=results,
        count=len(results),
        providers_used=["baidu"] if results else [],
        reranker="baidu-only",
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def _avg(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _p95(xs: List[int]) -> int:
    if not xs:
        return 0
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def _md(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _audit_flags_total(answer_audits: Dict[str, dict], engine_name: str) -> int:
    total = 0
    suffix = f":{engine_name}"
    for key, audit in answer_audits.items():
        if key.endswith(suffix) and audit:
            total += int(audit.get("flag_count", 0))
    return total


def build_report(
    rows: List[dict],
    answer_judgments: Dict[str, dict],
    args: argparse.Namespace,
    evidence_judgments: Optional[Dict[str, dict]] = None,
    answer_audits: Optional[Dict[str, dict]] = None,
) -> str:
    full_lat = [r["full_ms"] for r in rows]
    baidu_lat = [r["baidu_ms"] for r in rows]
    judged = bool(answer_judgments)
    evidence_judged = bool(evidence_judgments)
    wins = {"full_agent": 0, "baidu_only": 0, "tie": 0}
    if judged:
        for j in answer_judgments.values():
            wins[j["winner"]] += 1

    lines = [
        f"# Agent 场景对比报告 (n={len(rows)}, k={args.k})",
        "",
        "场景: 技术尽调/R&D 情报 agent。主分来自 agent 基于搜索结果写出的最终回答。",
        "",
        "## 总览",
        "| Metric | full_agent | baidu_only |",
        "|--------|------------|------------|",
        f"| avg_latency_ms | {_avg(full_lat):.0f} | {_avg(baidu_lat):.0f} |",
        f"| p95_latency_ms | {_p95(full_lat)} | {_p95(baidu_lat)} |",
        f"| avg_web_results | {_avg([r['full_web'] for r in rows]):.1f} | {_avg([r['baidu_web'] for r in rows]):.1f} |",
        f"| avg_academic_results | {_avg([r['full_academic'] for r in rows]):.1f} | 0.0 |",
        f"| avg_patent_results | {_avg([r['full_patent'] for r in rows]):.1f} | 0.0 |",
    ]
    if judged:
        lines += [
            f"| answer_wins | {wins['full_agent']} | {wins['baidu_only']} |",
            f"| answer_ties | {wins['tie']} | {wins['tie']} |",
            f"| avg_answer_score | {_avg([j['full_score'] for j in answer_judgments.values()]):.2f} | "
            f"{_avg([j['baidu_score'] for j in answer_judgments.values()]):.2f} |",
        ]
    if answer_audits:
        lines += [
            f"| answer_audit_flags | {_audit_flags_total(answer_audits, 'full_agent')} | "
            f"{_audit_flags_total(answer_audits, 'baidu_only')} |",
        ]
    if evidence_judged:
        ev_wins = {"full_agent": 0, "baidu_only": 0, "tie": 0}
        for j in evidence_judgments.values():
            ev_wins[j["winner"]] += 1
        lines += [
            f"| evidence_wins | {ev_wins['full_agent']} | {ev_wins['baidu_only']} |",
            f"| avg_evidence_score | {_avg([j['full_score'] for j in evidence_judgments.values()]):.2f} | "
            f"{_avg([j['baidu_score'] for j in evidence_judgments.values()]):.2f} |",
        ]

    lines += [
        "",
        "## 场景明细",
        "| ID | Domain | Evidence full(web/acad/pat) | Evidence baidu | Answer winner | Answer scores | Reason |",
        "|----|--------|-----------------------------|----------------|---------------|---------------|--------|",
    ]
    for r in rows:
        j = answer_judgments.get(r["id"], {})
        winner = j.get("winner", "N/A")
        scores = "N/A" if not j else f"{j['full_score']}:{j['baidu_score']}"
        full_blocks = f"{r['full_web']}/{r['full_academic']}/{r['full_patent']}"
        reason = j.get("reason", "")
        lines.append(
            f"| {_md(r['id'])} | {_md(r['domain'])} | {full_blocks} | {r['baidu_web']} | "
            f"{winner} | {scores} | {_md(reason)} |"
        )

    if judged:
        lines += [
            "",
            "## 回答质量维度",
            "| Dimension | full_agent | baidu_only |",
            "|-----------|------------|------------|",
            f"| grounding | {_avg([j['full_grounding'] for j in answer_judgments.values()]):.2f} / 2 | "
            f"{_avg([j['baidu_grounding'] for j in answer_judgments.values()]):.2f} / 2 |",
            f"| research | {_avg([j['full_research'] for j in answer_judgments.values()]):.2f} / 2 | "
            f"{_avg([j['baidu_research'] for j in answer_judgments.values()]):.2f} / 2 |",
            f"| patent | {_avg([j['full_patent'] for j in answer_judgments.values()]):.2f} / 2 | "
            f"{_avg([j['baidu_patent'] for j in answer_judgments.values()]):.2f} / 2 |",
            f"| synthesis | {_avg([j['full_synthesis'] for j in answer_judgments.values()]):.2f} / 2 | "
            f"{_avg([j['baidu_synthesis'] for j in answer_judgments.values()]):.2f} / 2 |",
        ]

    return "\n".join(lines) + "\n"


def write_details(
    scenarios: List[dict],
    pairs: List[Tuple[str, SearchResponse, SearchResponse]],
    answers: Dict[str, str],
    answer_audits: Dict[str, dict],
    answer_judgments: Dict[str, dict],
    evidence_judgments: Dict[str, dict],
    path: str,
    detail_k: int,
) -> None:
    scenario_by_id = {s["id"]: s for s in scenarios}
    details = []
    for sid, full, baidu in pairs:
        sc = scenario_by_id[sid]
        details.append({
            "id": sid,
            "domain": sc.get("domain", ""),
            "needs": sc.get("needs", []),
            "input": {
                "task": sc["task"],
            },
            "outputs": {
                "full_agent": compact_response(full, per_block_k=detail_k),
                "baidu_only": compact_response(baidu, per_block_k=detail_k),
            },
            "answers": {
                "full_agent": answers.get(f"{sid}:full_agent", ""),
                "baidu_only": answers.get(f"{sid}:baidu_only", ""),
            },
            "answer_audit": {
                "full_agent": answer_audits.get(f"{sid}:full_agent"),
                "baidu_only": answer_audits.get(f"{sid}:baidu_only"),
            },
            "answer_judge": answer_judgments.get(sid),
            "evidence_judge": evidence_judgments.get(sid),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="eval/agent_scenarios.jsonl")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--max-scenarios", type=int, default=0)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--no-answer", action="store_true", help="Only collect search diagnostics.")
    ap.add_argument("--judge-evidence", action="store_true", help="Also judge raw evidence bundles.")
    ap.add_argument("--answer-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--report-path", default=_REPORT_PATH)
    ap.add_argument("--details-path", default=_DETAILS_PATH)
    ap.add_argument("--detail-k", type=int, default=8)
    ap.add_argument("--answer-evidence-k", type=int, default=8)
    ap.add_argument(
        "--force-vertical",
        action="store_true",
        help="Force academic/patent in full_agent; default tests auto routing.",
    )
    args = ap.parse_args()

    if not settings.qianfan_api_key:
        raise SystemExit("缺少 QIANFAN_API_KEY,无法运行 baidu-only baseline")

    scenarios = load_scenarios(args.dataset, args.max_scenarios)
    engine = SearchEngine()
    baidu = BaiduSearchProvider(timeout=settings.provider_timeout)

    rows: List[dict] = []
    pairs: List[Tuple[str, SearchResponse, SearchResponse]] = []
    scenario_by_id = {s["id"]: s for s in scenarios}
    for idx, sc in enumerate(scenarios, 1):
        task = sc["task"]
        print(f"[{idx}/{len(scenarios)}] {sc['id']} full_agent...")
        full = run_full_agent(engine, task, args.k, args.force_vertical)
        print(f"      baidu_only...")
        baidu_resp = run_baidu_only(baidu, task, args.k)
        pairs.append((sc["id"], full, baidu_resp))
        rows.append({
            "id": sc["id"],
            "domain": sc.get("domain", ""),
            "full_ms": full.elapsed_ms,
            "baidu_ms": baidu_resp.elapsed_ms,
            "full_web": len(full.results),
            "full_academic": len(full.academic_results),
            "full_patent": len(full.patent_results),
            "baidu_web": len(baidu_resp.results),
        })
        print(
            f"      full web={len(full.results)} acad={len(full.academic_results)} "
            f"pat={len(full.patent_results)} {full.elapsed_ms}ms | "
            f"baidu web={len(baidu_resp.results)} {baidu_resp.elapsed_ms}ms"
        )

    answers: Dict[str, str] = {}
    answer_audits: Dict[str, dict] = {}
    answer_judgments: Dict[str, dict] = {}
    if not args.no_answer:
        print("\n[answer agent] generating final answers from each evidence bundle...")
        agent = EvidenceAnswerAgent(
            model=args.answer_model,
            evidence_k=args.answer_evidence_k,
        )
        answer_items = []
        for sid, full, baidu_resp in pairs:
            task = scenario_by_id[sid]["task"]
            answer_items.append((sid, "full_agent", task, full))
            answer_items.append((sid, "baidu_only", task, baidu_resp))
        answers = agent.generate_batch(answer_items)
        for sid, full, baidu_resp in pairs:
            answer_audits[f"{sid}:full_agent"] = answer_support_audit(
                answers.get(f"{sid}:full_agent", ""),
                full,
            )
            answer_audits[f"{sid}:baidu_only"] = answer_support_audit(
                answers.get(f"{sid}:baidu_only", ""),
                baidu_resp,
            )

        if not args.no_judge:
            print("\n[answer judge] comparing final answers...")
            judge = AnswerPairJudge(
                model=args.judge_model,
                evidence_k=args.answer_evidence_k,
            )
            answer_judgments = judge.score_batch([
                (
                    sid,
                    scenario_by_id[sid]["task"],
                    full,
                    answers.get(f"{sid}:full_agent", ""),
                    baidu_resp,
                    answers.get(f"{sid}:baidu_only", ""),
                )
                for sid, full, baidu_resp in pairs
            ])

    evidence_judgments: Dict[str, dict] = {}
    if args.judge_evidence:
        print("\n[evidence judge] comparing raw evidence bundles...")
        judge = ScenarioPairJudge(model=args.judge_model)
        evidence_judgments = judge.score_batch(pairs)

    report = build_report(rows, answer_judgments, args, evidence_judgments, answer_audits)
    print("\n" + report)
    with open(args.report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"-> wrote {args.report_path}")
    write_details(
        scenarios,
        pairs,
        answers,
        answer_audits,
        answer_judgments,
        evidence_judgments,
        args.details_path,
        args.detail_k,
    )
    print(f"-> wrote {args.details_path}")


if __name__ == "__main__":
    main()
