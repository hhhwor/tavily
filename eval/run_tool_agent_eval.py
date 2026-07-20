"""Evaluate a real tool-calling agent over the MCP search tools."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import timedelta
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import anyio

from eval.agent_answer_eval import AnswerPairJudge, EvidenceAnswerAgent, answer_support_audit
from eval.e2e_judge import compact_response, evidence_type_counts
from src.config import Settings
from src.interfaces.presenters import McpSearchPresenter
from src.models import SearchResponse
from src.domain.search_api import (
    QualityMix,
    RequestedFilters,
    RetrievalAssessment,
    RetrievalBoundary,
    SearchMeta,
    SearchQuery,
    SearchResultSet,
)

_REPORT_PATH = "eval/tool_agent_report.md"
_DETAILS_PATH = "eval/tool_agent_details.json"

settings = Settings()

_AGENT_SYSTEM = (
    "你是技术情报 agent。你必须按需调用可用搜索工具,不要凭记忆补外部事实。\n"
    "规则:\n"
    "- 需要外部事实、最新信息、论文、专利、公司/产业动态时,先调用 search。\n"
    "- 技术尽调/R&D 任务应尽量覆盖 web、academic、patent 三类证据。\n"
    "- search 结果会以 web1/academic1/patent1 等 ref 形式返回;最终回答只能引用这些 ref。\n"
    "- 必须检查 status、failures、retrieval_assessment.gaps;有缺口时明确说明。\n"
    "- 没有 academic evidence 时,不得把网页包装成论文证据;没有 patent evidence 时,不得把网页包装成专利证据。\n"
    "- 需要全文、反证或 claim 校验时,用 search_id 启动 research 并轮询任务。\n"
    "- 最终用中文输出紧凑报告,覆盖结论、证据、机会、风险和下一步建议。"
)


def load_scenarios(path: str, limit: int) -> List[dict]:
    rows: List[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


def _scenario_id(row: dict, idx: int) -> str: return str(row.get("id") or row.get("query") or f"scenario_{idx}")


def _scenario_task(row: dict) -> str:
    return str(row.get("task") or row.get("query") or "").strip()


def _scenario_needs(row: dict) -> List[str]:
    if row.get("needs"):
        return [str(x) for x in row["needs"]]
    qtype = row.get("type")
    if qtype == "academic": return ["academic"]
    if qtype == "patent": return ["patent"]
    return ["web"]


def _api_token(args: argparse.Namespace) -> str:
    token = args.api_token or os.getenv("EVAL_API_TOKEN") or settings.api_auth_token
    return token.split(",", 1)[0].strip() if token else ""


def _client(model_api_key: Optional[str] = None):
    import anthropic

    key = model_api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("缺少 ANTHROPIC_API_KEY")
    kwargs = {"api_key": key}
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url: kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def _tools_schema() -> List[dict]:
    search_props = {
        "query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        "source_types": {"type": "array", "items": {"type": "string", "enum": ["web", "academic", "patent"]}},
        "filters": {"type": "object"},
    }
    return [
        {
            "name": "search",
            "description": (
                "调用真实 MCP search 工具,返回按 web/academic/patent ref 压缩后的证据包。"
                "技术尽调任务可显式声明 source_types。"
            ),
            "input_schema": {"type": "object", "properties": search_props, "required": ["query"]},
        },
        {
            "name": "research",
            "description": "启动、读取、反馈或取消可信研究任务。先 search，再以 search_id start。",
            "input_schema": {"type": "object", "properties": {
                "operation": {"type": "string", "enum": ["start", "get", "feedback", "cancel"]},
                "search_id": {"type": "string"},
                "research_id": {"type": "string"},
                "profile": {"type": "string"},
                "depth": {"type": "string", "enum": ["quick", "standard", "deep"]},
                "detail": {"type": "string", "enum": ["standard", "full"]},
            }, "required": ["operation"]},
        },
    ]


def _assistant_blocks(message: Any) -> List[dict]:
    blocks: List[dict] = []
    for block in message.content:
        btype = getattr(block, "type", "")
        if btype == "text":
            blocks.append({"type": "text", "text": getattr(block, "text", "")})
        elif btype == "tool_use":
            blocks.append({"type": "tool_use", "id": getattr(block, "id", ""),
                           "name": getattr(block, "name", ""),
                           "input": getattr(block, "input", {}) or {}})
    return blocks


def _text_from_blocks(blocks: List[dict]) -> str:
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _mcp_result_json(result: Any) -> dict:
    text_parts = []
    for part in getattr(result, "content", []) or []:
        if getattr(part, "type", "") == "text":
            text_parts.append(getattr(part, "text", ""))
    text = "".join(text_parts).strip()
    if not text: return {}
    return json.loads(text)


def _search_response_from_mcp(data: dict) -> SearchResponse:
    return McpSearchPresenter.restore(data)


def _merge_responses(task: str, responses: List[SearchResponse]) -> SearchResponse:
    if not responses:
        return SearchResponse(
            request_id="req_tool_agent_empty",
            status="complete",
            research_seed=None,
            query=SearchQuery(
                original=task,
                effective=task,
                filters_requested=RequestedFilters(),
            ),
            result_set=SearchResultSet(returned=0, limit=10),
            retrieval_assessment=RetrievalAssessment(
                status="unusable", quality_mix=QualityMix()
            ),
            retrieval_boundary=RetrievalBoundary(
                query_time=datetime.now(timezone.utc), deadline_ms=0
            ),
            meta=SearchMeta(elapsed_ms=0),
        )
    evidence = []
    failures = []
    for resp in responses:
        evidence.extend(resp.evidence)
        failures.extend(resp.failures)
    last = responses[-1]
    return SearchResponse(
        request_id=last.request_id,
        status="partial" if any(r.status == "partial" for r in responses) else "complete",
        research_seed=last.research_seed,
        query=last.query.model_copy(update={"original": task}),
        evidence=evidence,
        failures=failures,
        result_set=SearchResultSet(returned=len(evidence), limit=len(evidence)),
        retrieval_assessment=last.retrieval_assessment,
        retrieval_boundary=last.retrieval_boundary,
        meta=SearchMeta(elapsed_ms=sum(r.meta.elapsed_ms for r in responses)),
    )


def _has_gap_disclosure(answer: str) -> bool:
    gap_terms = ("不足", "缺少", "缺失", "未检索", "没有", "无法", "缺口", "超时", "失败")
    return any(term in answer for term in gap_terms)


def _coverage(needs: List[str], counts: Dict[str, int]) -> float:
    required = [n for n in needs if n in {"web", "academic", "patent"}]
    if not required:
        return 1.0
    return sum(1 for n in required if counts.get(n, 0) > 0) / len(required)


class McpToolAgent:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = _client(args.anthropic_api_key)

    async def run_one(self, session: Any, row: dict, idx: int) -> dict:
        sid = _scenario_id(row, idx)
        task = _scenario_task(row)
        needs = _scenario_needs(row)
        messages: List[dict] = [{"role": "user", "content": task}]
        transcript: List[dict] = [{"role": "user", "content": task}]
        tool_events: List[dict] = []
        search_responses: List[SearchResponse] = []
        started = time.time()

        for _ in range(self.args.max_tool_calls + 1):
            if len(tool_events) >= self.args.max_tool_calls:
                notice = "工具调用预算已用完。请立即基于已有工具结果输出最终答案,不要再请求工具。"
                messages.append({"role": "user", "content": notice}); transcript.append({"role": "user", "content": notice})
                msg = self.client.messages.create(
                    model=self.args.model, max_tokens=self.args.max_tokens,
                    system=[{"type": "text", "text": _AGENT_SYSTEM}], messages=messages
                )
                final_blocks = _assistant_blocks(msg)
                messages.append({"role": "assistant", "content": final_blocks})
                transcript.append({"role": "assistant", "content": final_blocks})
                break
            msg = self.client.messages.create(
                model=self.args.model,
                max_tokens=self.args.max_tokens,
                system=[{"type": "text", "text": _AGENT_SYSTEM}],
                messages=messages,
                tools=_tools_schema(),
            )
            assistant_blocks = _assistant_blocks(msg)
            messages.append({"role": "assistant", "content": assistant_blocks})
            transcript.append({"role": "assistant", "content": assistant_blocks})
            tool_uses = [b for b in assistant_blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                break

            tool_results = []
            for tool in tool_uses:
                if len(tool_events) >= self.args.max_tool_calls:
                    tool_results.append({"type": "tool_result", "tool_use_id": tool["id"],
                                         "content": "工具调用预算已用完,该工具请求未执行。"})
                    continue
                event, content = await self._call_mcp_tool(session, tool, search_responses)
                tool_events.append(event)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool["id"],
                    "content": json.dumps(content, ensure_ascii=False),
                })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                transcript.append({"role": "user", "content": tool_results})

        answer = _text_from_blocks(transcript[-1].get("content", [])) if transcript else ""
        aggregate = _merge_responses(task, search_responses)
        audit = answer_support_audit(answer, aggregate)
        counts = evidence_type_counts(aggregate)
        material_gaps = [g for g in aggregate.retrieval_assessment.gaps if getattr(g, "severity", "") in {"warning", "blocking"}]
        has_gaps = bool(aggregate.status == "partial" or aggregate.failures or material_gaps)
        stats = {
            "tool_calls": len(tool_events),
            "search_calls": sum(1 for e in tool_events if e["tool"] == "search"),
            "research_calls": sum(1 for e in tool_events if e["tool"] == "research"),
            "tool_call_rate": 1.0 if tool_events else 0.0,
            "tool_latency_ms": sum(int(e.get("latency_ms", 0)) for e in tool_events),
            "elapsed_ms": int((time.time() - started) * 1000),
            "counts": counts,
            "required_source_coverage": _coverage(needs, counts),
            "partial_failure": aggregate.status == "partial",
            "failure_count": len(aggregate.failures),
            "has_gaps": has_gaps,
            "gap_disclosed": (not has_gaps) or _has_gap_disclosure(answer),
            "support_audit_flags": int(audit.get("flag_count", 0)),
        }
        return {
            "id": sid,
            "domain": row.get("domain", row.get("type", "")),
            "needs": needs,
            "task": task,
            "answer": answer,
            "stats": stats,
            "support_audit": audit,
            "final_evidence": compact_response(aggregate, per_block_k=self.args.detail_k),
            "tool_events": tool_events,
            "transcript": transcript,
            "_aggregate_response": aggregate,
        }

    async def _call_mcp_tool(
        self,
        session: Any,
        tool: dict,
        search_responses: List[SearchResponse],
    ) -> Tuple[dict, dict]:
        name = tool["name"]
        arguments = dict(tool.get("input") or {})
        started = time.time()
        raw = await session.call_tool(
            name, arguments, read_timeout_seconds=timedelta(seconds=self.args.tool_timeout)
        )
        latency_ms = int((time.time() - started) * 1000)
        data = _mcp_result_json(raw)
        event: Dict[str, Any] = {"tool": name, "arguments": arguments, "latency_ms": latency_ms,
                                 "is_error": bool(getattr(raw, "isError", False))}
        if name == "search" and isinstance(data, dict):
            resp = _search_response_from_mcp(data)
            search_responses.append(resp)
            compact = compact_response(resp, per_block_k=self.args.evidence_k)
            event["summary"] = {"counts": compact["counts"], "partial_failure": compact["partial_failure"],
                                "failures": compact["failures"], "elapsed_ms": compact["elapsed_ms"]}
            return event, compact
        if name == "research" and isinstance(data, dict):
            event["summary"] = {
                "research_id": data.get("research_id"),
                "state": data.get("state"),
                "phase": data.get("phase"),
                "assessment": (data.get("dossier") or {}).get("assessment"),
                "stop": data.get("stop"),
            }
            return event, data
        return event, data


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _p95(values: List[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def build_report(details: List[dict], judgments: Dict[str, dict], args: argparse.Namespace) -> str:
    stats = [d["stats"] for d in details]
    lines = [
        f"# Tool Agent E2E 报告 (n={len(details)})",
        "",
        f"- mcp_url: `{args.mcp_url}`",
        f"- model: `{args.model}`",
        f"- judge: {'off' if args.no_judge else args.judge_model}",
        "",
        "## 总览",
        "| Metric | Value |",
        "|--------|-------|",
        f"| tool_call_rate | {_avg([s['tool_call_rate'] for s in stats]):.3f} |",
        f"| avg_tool_calls | {_avg([s['tool_calls'] for s in stats]):.2f} |",
        f"| avg_required_source_coverage | {_avg([s['required_source_coverage'] for s in stats]):.3f} |",
        f"| total_support_audit_flags | {sum(s['support_audit_flags'] for s in stats)} |",
        f"| gap_disclosure_rate | {_avg([1.0 if s['gap_disclosed'] else 0.0 for s in stats]):.3f} |",
        f"| p95_elapsed_ms | {_p95([s['elapsed_ms'] for s in stats])} |",
        "",
        "## 场景明细",
        "| ID | Domain | Tools(search/pdf) | Evidence(web/acad/pat) | Coverage | Partial | AuditFlags | Judge |",
        "|----|--------|-------------------|------------------------|----------|---------|------------|-------|",
    ]
    for d in details:
        s = d["stats"]
        c = s["counts"]
        j = judgments.get(d["id"], {})
        judge_text = "N/A" if not j else f"{j.get('winner')} {j.get('full_score')}:{j.get('baidu_score')}"
        lines.append(
            f"| {_md(d['id'])} | {_md(d.get('domain', ''))} | "
            f"{s['tool_calls']}({s['search_calls']}/{s['research_calls']}) | "
            f"{c['web']}/{c['academic']}/{c['patent']} | "
            f"{s['required_source_coverage']:.2f} | {int(s['partial_failure'])} | "
            f"{s['support_audit_flags']} | {_md(judge_text)} |"
        )
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> Tuple[List[dict], Dict[str, dict]]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {}
    token = _api_token(args)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    scenarios = load_scenarios(args.dataset, args.max_scenarios)
    agent = McpToolAgent(args)
    details: List[dict] = []

    async with streamablehttp_client(
        args.mcp_url,
        headers=headers or None,
        timeout=args.mcp_timeout,
        sse_read_timeout=args.mcp_sse_timeout,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for idx, row in enumerate(scenarios, 1):
                sid = _scenario_id(row, idx)
                print(f"[{idx}/{len(scenarios)}] {sid} tool-agent...")
                details.append(await agent.run_one(session, row, idx))

    judgments: Dict[str, dict] = {}
    if not args.no_judge:
        judgments = _judge_against_baidu(details, args)
    return details, judgments


def _judge_against_baidu(details: List[dict], args: argparse.Namespace) -> Dict[str, dict]:
    from eval.run_agent_scenario_compare import run_baidu_only
    from src.providers.baidu import BaiduSearchProvider

    baidu = BaiduSearchProvider(
        api_key=settings.qianfan_api_key,
        timeout=settings.provider_timeout,
    )
    answer_agent = EvidenceAnswerAgent(model=args.answer_model, evidence_k=args.evidence_k)
    baseline_items = []
    baidu_responses: Dict[str, SearchResponse] = {}
    for d in details:
        resp = run_baidu_only(baidu, d["task"], args.k)
        baidu_responses[d["id"]] = resp
        baseline_items.append((d["id"], "baidu_only", d["task"], resp))
    baidu_answers = answer_agent.generate_batch(baseline_items)
    judge = AnswerPairJudge(model=args.judge_model, evidence_k=args.evidence_k)
    return judge.score_batch([
        (
            d["id"],
            d["task"],
            d["_aggregate_response"],
            d["answer"],
            baidu_responses[d["id"]],
            baidu_answers.get(f"{d['id']}:baidu_only", ""),
        )
        for d in details
    ])


def _public_details(details: List[dict]) -> List[dict]:
    out = []
    for d in details:
        clean = dict(d)
        clean.pop("_aggregate_response", None)
        out.append(clean)
    return out


def main() -> None:
    global settings
    settings = Settings.from_env()
    ap = argparse.ArgumentParser()
    for name, default in [
        ("--dataset", "eval/agent_scenarios.jsonl"),
        ("--mcp-url", "http://127.0.0.1:8000/mcp"),
        ("--api-token", ""),
        ("--model", "claude-haiku-4-5-20251001"),
        ("--anthropic-api-key", ""),
        ("--answer-model", "claude-haiku-4-5-20251001"),
        ("--judge-model", "claude-haiku-4-5-20251001"),
        ("--report-path", _REPORT_PATH),
        ("--details-path", _DETAILS_PATH),
    ]:
        ap.add_argument(name, default=default)
    for name, default in [
        ("--max-scenarios", 0),
        ("--max-tokens", 1800),
        ("--max-tool-calls", 4),
        ("--tool-timeout", 90),
        ("--k", 8),
        ("--evidence-k", 8),
        ("--detail-k", 8),
        ("--pdf-result-chars", 3000),
    ]:
        ap.add_argument(name, type=int, default=default)
    ap.add_argument("--mcp-timeout", type=float, default=30)
    ap.add_argument("--mcp-sse-timeout", type=float, default=300)
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    details, judgments = anyio.run(_run, args)
    report = build_report(details, judgments, args)
    print("\n" + report)
    with open(args.report_path, "w", encoding="utf-8") as f:
        f.write(report)
    with open(args.details_path, "w", encoding="utf-8") as f:
        json.dump({"details": _public_details(details), "answer_judgments": judgments}, f, ensure_ascii=False, indent=2)
    print(f"-> wrote {args.report_path}")
    print(f"-> wrote {args.details_path}")


if __name__ == "__main__":
    main()
