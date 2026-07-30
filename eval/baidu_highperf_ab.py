"""20-query A/B probe for Baidu web_search vs web_summary.

The runner deliberately keeps four concerns separate:

1. retrieval coverage: current ``web_search`` vs high-performance ``web_summary``;
2. ranking quality: Baidu rerank_score vs SiliconFlow on the same candidates;
3. transport/product behavior: full-content flag, SSE first-reference latency, QPS;
4. answer quality: native web_summary answer vs an answer generated from the
   current Baidu + SiliconFlow evidence bundle.

All network results are checkpointed after every query so reruns do not consume
the daily quota again. Credentials are read from the normal project settings and
are never written to the artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from eval import metrics as M
from src.config import Settings
from src.pipeline.dedup import normalize_url
from src.ranking.adapters.siliconflow import SiliconFlowReranker


_CURRENT_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
_HIGHPERF_URL = "https://qianfan.baidubce.com/v2/ai_search/web_summary"
_CACHE_VERSION = 1
_ANSWER_INSTRUCTION = (
    "请严格基于检索结果准确、简洁地回答；关键结论标注网页编号；"
    "证据不足或信息不确定时明确说明。"
)

_REL_RUBRIC = """你是搜索相关性评审。给定一个查询和若干网页，逐条判断网页对回答查询的相关程度。
只能根据给出的标题、日期和摘要判分，不要补充外部知识。

3=高度相关，直接且充分支持回答核心意图
2=相关，包含直接有用信息但不够完整
1=略相关，只是沾边或信息很弱
0=不相关、广告、导航或无效内容

只输出 JSON：{"labels":[{"id":0,"rel":0}]}。必须覆盖输入中的每个 id。"""

_BASELINE_ANSWER_PROMPT = """你是检索增强问答助手。请只使用给出的 evidence 回答 query，不得补充外部事实。
要求：
- 中文回答，完整但尽量紧凑；
- 每个关键事实使用 [1]、[2] 形式引用；
- 引用编号必须来自 evidence.id；
- 对时效问题优先使用日期较新的证据；
- 证据不足时明确说明，不得猜测。"""

_ANSWER_PAIR_RUBRIC = """你是联网问答质量评审。比较同一个 query 的回答 A 和回答 B。
每个回答都附带它实际使用的证据。只根据给出的证据评估，不要使用外部知识。

重点评估：
- correctness：回答陈述是否被证据支持；
- completeness：是否覆盖查询核心意图；
- grounding：引用是否存在且真正支持相邻结论；
- freshness：时效问题是否使用足够新的证据；非时效问题按正常质量评估。

请避免位置偏好，不因篇幅更长而自动给高分。只输出 JSON：
{"winner":"A|B|tie","a_score":0,"b_score":0,
 "a_correctness":0,"b_correctness":0,
 "a_completeness":0,"b_completeness":0,
 "a_grounding":0,"b_grounding":0,
 "a_freshness":0,"b_freshness":0,"reason":"不超过80字"}

总分范围 0-10；四个分项范围 0-2。"""


def _read_project_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    values.update(os.environ)
    return values


def _load_queries(path: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") in {"academic", "patent"}:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    if len(rows) != limit:
        raise ValueError(f"需要 {limit} 条通用 Query，数据集只找到 {len(rows)} 条")
    return rows


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Appbuilder-Request-Id": str(uuid.uuid4()),
    }


def _error_payload(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        body = {"text": response.text[:500]}
    return {
        "ok": False,
        "status": response.status_code,
        "error": body,
    }


def _current_search(
    session: requests.Session,
    api_key: str,
    query: str,
    top_k: int,
) -> dict[str, Any]:
    body = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": top_k}],
    }
    started = time.perf_counter()
    response = session.post(
        _CURRENT_URL,
        headers=_headers(api_key),
        json=body,
        timeout=(10, 60),
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if response.status_code != 200:
        return {**_error_payload(response), "elapsed_ms": elapsed_ms}
    data = response.json()
    return {
        "ok": isinstance(data.get("references"), list),
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "request_id": data.get("request_id") or data.get("requestId"),
        "references": data.get("references") or [],
        "error": None if "references" in data else data,
    }


def _append_choice_text(event: dict[str, Any], answer: list[str], reasoning: list[str]) -> bool:
    added = False
    for choice in event.get("choices") or []:
        payload = choice.get("delta") or choice.get("message") or {}
        content = payload.get("content")
        if content:
            answer.append(str(content))
            added = True
        thought = payload.get("reasoning_content")
        if thought:
            reasoning.append(str(thought))
    return added


def _highperf_search(
    session: requests.Session,
    api_key: str,
    query: str,
    top_k: int,
    *,
    full_content: bool,
    stream: bool,
    instruction: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "instruction": instruction or _ANSWER_INSTRUCTION,
        "messages": [{"role": "user", "content": query}],
        "stream": stream,
        "resource_type_filter": [{"type": "web", "top_k": top_k}],
        "temperature": 1e-6,
        "top_p": 1e-10,
        "model": "non_thinking",
        # The response schema documents this switch even though the request
        # parameter table currently omits it. The probe intentionally sends it.
        "enable_full_content": full_content,
    }
    started = time.perf_counter()
    response = session.post(
        _HIGHPERF_URL,
        headers=_headers(api_key),
        json=body,
        timeout=(10, 180),
        stream=stream,
    )
    headers_ms = round((time.perf_counter() - started) * 1000, 1)
    if response.status_code != 200:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            **_error_payload(response),
            "elapsed_ms": elapsed_ms,
            "headers_ms": headers_ms,
            "requested_full_content": full_content,
            "stream": stream,
        }

    if not stream:
        data = response.json()
        return {
            "ok": isinstance(data.get("references"), list),
            "status": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "headers_ms": headers_ms,
            "first_event_ms": None,
            "first_reference_ms": None,
            "first_token_ms": None,
            "request_id": data.get("request_id") or data.get("requestId"),
            "references": data.get("references") or [],
            "answer": "".join(
                str((choice.get("message") or {}).get("content") or "")
                for choice in data.get("choices") or []
            ),
            "reasoning": "".join(
                str((choice.get("message") or {}).get("reasoning_content") or "")
                for choice in data.get("choices") or []
            ),
            "requested_full_content": full_content,
            "stream": stream,
            "error": None if "references" in data else data,
        }

    first_event_ms: float | None = None
    first_reference_ms: float | None = None
    first_token_ms: float | None = None
    references: list[dict[str, Any]] = []
    answer: list[str] = []
    reasoning: list[str] = []
    request_id: str | None = None
    event_count = 0
    parse_errors: list[str] = []
    response.encoding = "utf-8"

    def consume(line: str) -> None:
        nonlocal event_count, first_event_ms, first_reference_ms, first_token_ms
        nonlocal references, request_id
        line = line.strip()
        if not line or line == "[DONE]":
            return
        event_count += 1
        now_ms = round((time.perf_counter() - started) * 1000, 1)
        if first_event_ms is None:
            first_event_ms = now_ms
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if len(parse_errors) < 3:
                parse_errors.append(line[:200])
            return
        request_id = request_id or event.get("request_id") or event.get("requestId")
        if event.get("references") and not references:
            references = event["references"]
            first_reference_ms = now_ms
        before = len(answer)
        _append_choice_text(event, answer, reasoning)
        if len(answer) > before and first_token_ms is None:
            first_token_ms = now_ms

    # The first event can contain all references and Baidu may format that JSON
    # across multiple physical SSE lines. Assemble until the blank event
    # delimiter instead of assuming one JSON object per line.
    event_lines: list[str] = []
    for raw in response.iter_lines(decode_unicode=True):
        if raw == "":
            if event_lines:
                consume("\n".join(event_lines))
                event_lines.clear()
            continue
        line = raw
        if line.startswith("data:"):
            line = line[5:].lstrip()
        event_lines.append(line)
    if event_lines:
        consume("\n".join(event_lines))

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "ok": bool(references) and not parse_errors,
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "headers_ms": headers_ms,
        "first_event_ms": first_event_ms,
        "first_reference_ms": first_reference_ms,
        "first_token_ms": first_token_ms,
        "event_count": event_count,
        "request_id": request_id,
        "references": references,
        "answer": "".join(answer),
        "reasoning": "".join(reasoning),
        "requested_full_content": full_content,
        "stream": stream,
        "parse_errors": parse_errors,
        "error": None if references else {"message": "SSE 未返回 references"},
    }


def _ref_key(ref: dict[str, Any], index: int = 0) -> str:
    url = str(ref.get("url") or "")
    return normalize_url(url) or url or f"title:{ref.get('title', '')}:{index}"


def _ref_text(ref: dict[str, Any], limit: int = 2000) -> str:
    content = str(ref.get("content") or ref.get("snippet") or "")
    return f"{ref.get('title', '')}\n{content[:limit]}".strip()


def _dedup_refs(groups: Iterable[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for refs in groups:
        for index, ref in enumerate(refs):
            key = _ref_key(ref, index)
            if key not in chosen:
                chosen[key] = dict(ref)
                order.append(key)
            elif len(_ref_text(ref)) > len(_ref_text(chosen[key])):
                chosen[key] = dict(ref)
    return [chosen[key] for key in order]


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = lo
    return max(lo, min(hi, number))


def _json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"模型未返回 JSON: {text[:200]}")
    return json.loads(match.group())


def _loose_pair_judge_object(text: str) -> dict[str, Any] | None:
    """Recover the fixed score fields when only the free-text reason is invalid.

    Some judge responses contain an otherwise valid object but put unescaped
    quotation marks in the final ``reason`` string.  The fields below are a
    closed schema, so extracting all of them is safer than trying to rewrite
    arbitrary JSON text.  This fallback deliberately does not apply to other
    judge schemas.
    """
    winner_match = re.search(r'"winner"\s*:\s*"(A|B|tie)"', text)
    numeric_fields = (
        "a_score", "b_score",
        "a_correctness", "b_correctness",
        "a_completeness", "b_completeness",
        "a_grounding", "b_grounding",
        "a_freshness", "b_freshness",
    )
    values: dict[str, Any] = {}
    for field in numeric_fields:
        match = re.search(rf'"{field}"\s*:\s*(-?\d+)', text)
        if not match:
            return None
        values[field] = int(match.group(1))
    if not winner_match:
        return None
    values["winner"] = winner_match.group(1)
    values["reason"] = "裁判评分字段完整；原始 reason 含未转义引号，已省略"
    return values


def _ask_json(
    claude: "ClaudeClient",
    system: str,
    payload: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    last_text = ""
    last_error: Exception | None = None
    for attempt in range(3):
        retry_payload = dict(payload)
        if attempt:
            retry_payload["output_correction"] = (
                "上一次输出不是合法 JSON。只输出一个语法完整的 JSON 对象，"
                "reason 内不要使用未转义引号。"
            )
        last_text = claude.ask(system, retry_payload, max_tokens)
        try:
            return _json_object(last_text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
    recovered = _loose_pair_judge_object(last_text)
    if recovered is not None:
        return recovered
    raise ValueError(f"judge 连续返回非法 JSON: {last_text[:500]}") from last_error


class ClaudeClient:
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        import anthropic

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model

    def ask(self, system: str, payload: dict[str, Any], max_tokens: int) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=0,
                    system=[{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    }],
                )
                return "".join(
                    block.text for block in message.content if block.type == "text"
                ).strip()
            except Exception as exc:  # pragma: no cover - live network retry
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        assert last_error is not None
        raise last_error


def _judge_relevance(
    claude: ClaudeClient,
    query: str,
    pool: Sequence[dict[str, Any]],
) -> dict[str, int]:
    documents = []
    keys = []
    for index, ref in enumerate(pool):
        key = _ref_key(ref, index)
        keys.append(key)
        documents.append({
            "id": index,
            "title": str(ref.get("title") or "")[:180],
            "date": str(ref.get("date") or "")[:40],
            "url": str(ref.get("url") or "")[:500],
            "text": str(ref.get("snippet") or ref.get("content") or "")[:700],
        })
    data = _ask_json(
        claude, _REL_RUBRIC, {"query": query, "documents": documents}, 1200
    )
    labels = {int(item["id"]): _clamp_int(item.get("rel"), 0, 3)
              for item in data.get("labels") or [] if "id" in item}
    if set(labels) != set(range(len(documents))):
        missing = sorted(set(range(len(documents))) - set(labels))
        raise ValueError(f"相关性 judge 缺失文档 id: {missing}")
    return {key: labels[index] for index, key in enumerate(keys)}


def _evidence(refs: Sequence[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    out = []
    for index, ref in enumerate(refs[:limit], 1):
        out.append({
            "id": index,
            "title": str(ref.get("title") or "")[:200],
            "url": str(ref.get("url") or "")[:500],
            "date": str(ref.get("date") or "")[:50],
            "website": str(ref.get("website") or ref.get("web_anchor") or "")[:100],
            "text": str(ref.get("content") or ref.get("snippet") or "")[:1400],
        })
    return out


def _baseline_answer(
    claude: ClaudeClient,
    query: str,
    refs: Sequence[dict[str, Any]],
) -> str:
    return claude.ask(
        _BASELINE_ANSWER_PROMPT,
        {"query": query, "evidence": _evidence(refs)},
        1400,
    )


def _judge_answer_pair(
    claude: ClaudeClient,
    query: str,
    current_answer: str,
    current_refs: Sequence[dict[str, Any]],
    high_answer: str,
    high_refs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    # Deterministically alternate positions to reduce position bias.
    high_is_a = int(hashlib.sha1(query.encode("utf-8")).hexdigest(), 16) % 2 == 0
    current_payload = {
        "answer": current_answer,
        "evidence": _evidence(current_refs),
    }
    high_payload = {
        "answer": high_answer,
        "evidence": _evidence(high_refs),
    }
    payload = {
        "query": query,
        "A": high_payload if high_is_a else current_payload,
        "B": current_payload if high_is_a else high_payload,
    }
    raw = _ask_json(claude, _ANSWER_PAIR_RUBRIC, payload, 700)
    winner = str(raw.get("winner", "tie"))
    if winner not in {"A", "B", "tie"}:
        winner = "tie"

    def field(side: str, name: str, hi: int) -> int:
        return _clamp_int(raw.get(f"{side.lower()}_{name}"), 0, hi)

    a = {
        "score": field("A", "score", 10),
        "correctness": field("A", "correctness", 2),
        "completeness": field("A", "completeness", 2),
        "grounding": field("A", "grounding", 2),
        "freshness": field("A", "freshness", 2),
    }
    b = {
        "score": field("B", "score", 10),
        "correctness": field("B", "correctness", 2),
        "completeness": field("B", "completeness", 2),
        "grounding": field("B", "grounding", 2),
        "freshness": field("B", "freshness", 2),
    }
    current = b if high_is_a else a
    high = a if high_is_a else b
    mapped_winner = "tie"
    if winner != "tie":
        won_high = (winner == "A") == high_is_a
        mapped_winner = "highperf" if won_high else "current_sf"
    return {
        "winner": mapped_winner,
        "current_sf": current,
        "highperf": high,
        "reason": str(raw.get("reason") or "")[:160],
        "high_was_position": "A" if high_is_a else "B",
    }


def _score_union(
    reranker: SiliconFlowReranker,
    query: str,
    pool: Sequence[dict[str, Any]],
) -> dict[str, float]:
    texts = [_ref_text(ref) for ref in pool]
    scores = reranker.score(query, texts)
    return {_ref_key(ref, index): float(score)
            for index, (ref, score) in enumerate(zip(pool, scores))}


def _rank_refs(
    refs: Sequence[dict[str, Any]],
    scores: dict[str, float] | None,
    *,
    baidu: bool = False,
) -> list[dict[str, Any]]:
    copied = [dict(ref) for ref in refs]
    if baidu:
        return sorted(
            copied,
            key=lambda ref: float(ref.get("rerank_score") or -1.0),
            reverse=True,
        )
    if scores is None:
        return copied
    return sorted(
        copied,
        key=lambda ref: scores.get(_ref_key(ref), -1.0),
        reverse=True,
    )


def _metrics_for(
    refs: Sequence[dict[str, Any]],
    labels: dict[str, int],
    pool_refs: Sequence[dict[str, Any]],
    k: int,
) -> dict[str, float]:
    ranked_rels = [labels.get(_ref_key(ref, index), 0)
                   for index, ref in enumerate(refs)]
    pool_rels = [labels.get(_ref_key(ref, index), 0)
                 for index, ref in enumerate(pool_refs)]
    return {
        "ndcg": M.ndcg_at_k(ranked_rels, pool_rels, k),
        "recall": M.recall_at_k(ranked_rels, pool_rels, k),
        "precision": M.precision_at_k(ranked_rels, k),
        "mrr": M.mrr(ranked_rels),
    }


def _mean_dict(rows: Sequence[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {}
    return {key: statistics.fmean(row[key] for row in rows) for key in rows[0]}


def _paired_bootstrap(differences: Sequence[float], seed: int = 20260723) -> dict[str, Any]:
    """Return a deterministic query-level paired bootstrap interval."""
    if not differences:
        return {"mean_delta": None, "ci95": [None, None], "wins_ties_losses": [0, 0, 0]}
    rng = random.Random(seed)
    size = len(differences)
    samples = sorted(
        statistics.fmean(rng.choice(differences) for _ in range(size))
        for _ in range(20_000)
    )
    return {
        "mean_delta": statistics.fmean(differences),
        "ci95": [samples[int(0.025 * len(samples))], samples[int(0.975 * len(samples))]],
        "wins_ties_losses": [
            sum(value > 1e-12 for value in differences),
            sum(abs(value) <= 1e-12 for value in differences),
            sum(value < -1e-12 for value in differences),
        ],
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return float(ordered[index])


def _rankdata(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        rank = (position + end - 1) / 2 + 1
        for cursor in range(position, end):
            ranks[order[cursor]] = rank
        position = end
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return numerator / (dx * dy) if dx and dy else None


def _spearman_for(
    refs: Sequence[dict[str, Any]],
    sf_scores: dict[str, float],
) -> float | None:
    pairs = [
        (float(ref["rerank_score"]), sf_scores.get(_ref_key(ref), 0.0))
        for ref in refs
        if ref.get("rerank_score") is not None
    ]
    if len(pairs) < 2:
        return None
    baidu, sf = zip(*pairs)
    return _pearson(_rankdata(baidu), _rankdata(sf))


def _top_overlap(left: Sequence[dict[str, Any]], right: Sequence[dict[str, Any]], k: int) -> float:
    a = {_ref_key(ref, i) for i, ref in enumerate(left[:k])}
    b = {_ref_key(ref, i) for i, ref in enumerate(right[:k])}
    return len(a & b) / k if k else 0.0


def _content_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    lengths: list[int] = []
    snippet_lengths: list[int] = []
    longer = 0
    same = 0
    for row in rows:
        for ref in row.get("references") or []:
            content = str(ref.get("content") or "")
            snippet = str(ref.get("snippet") or "")
            lengths.append(len(content))
            snippet_lengths.append(len(snippet))
            longer += int(bool(content) and len(content) > len(snippet))
            same += int(bool(content) and content == snippet)
    return {
        "count": len(lengths),
        "mean_content_chars": statistics.fmean(lengths) if lengths else 0.0,
        "median_content_chars": statistics.median(lengths) if lengths else 0.0,
        "p95_content_chars": _percentile(lengths, 0.95) or 0.0,
        "max_content_chars": max(lengths, default=0),
        "mean_snippet_chars": statistics.fmean(snippet_lengths) if snippet_lengths else 0.0,
        "content_longer_than_snippet": longer,
        "content_equals_snippet": same,
    }


def _run_qps_stage(
    api_key: str,
    rate: float,
    count: int,
) -> dict[str, Any]:
    barrier = threading.Barrier(count)
    start = time.perf_counter()

    def work(index: int) -> dict[str, Any]:
        session = requests.Session()
        barrier.wait()
        target = start + index / rate
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        issued = time.perf_counter()
        result = _highperf_search(
            session,
            api_key,
            f"北京天气 QPS探针{rate:g}-{index}",
            1,
            full_content=False,
            stream=False,
        )
        session.close()
        return {
            "index": index,
            "issued_ms": round((issued - start) * 1000, 1),
            "status": result.get("status"),
            "ok": result.get("ok", False),
            "elapsed_ms": result.get("elapsed_ms"),
            "error": result.get("error"),
        }

    with ThreadPoolExecutor(max_workers=count) as executor:
        results = list(executor.map(work, range(count)))
    return {
        "target_qps": rate,
        "request_count": count,
        "wall_ms": round((time.perf_counter() - start) * 1000, 1),
        "successes": sum(bool(item["ok"]) for item in results),
        "statuses": {str(status): sum(item["status"] == status for item in results)
                     for status in sorted({item["status"] for item in results}, key=str)},
        "requests": results,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _report(summary: dict[str, Any], details: dict[str, Any], args: argparse.Namespace) -> str:
    retrieval = summary["retrieval_metrics_union_pool"]
    ranking = summary["ranking_metrics_high_pool"]
    latency = summary["latency_ms"]
    content = summary["content"]
    answers = summary["answer_quality"]
    checks = summary["statistical_checks"]
    ndcg_check = checks["same_pool_sf_minus_baidu_ndcg"]
    recall_check = checks["same_pool_sf_minus_baidu_recall"]
    lines = [
        f"# 百度高性能版 A/B 报告（n={summary['query_count']}，k={args.metric_k}）",
        "",
        f"- generated_at_utc: `{summary['generated_at_utc']}`",
        "- A: 当前百度 `/web_search` + SiliconFlow 重排 + 固定证据回答器",
        "- B: 百度高性能 `/web_summary` 原生排序 + 原生答案",
        "- relevance: 同 Query 的 A/B URL 并集，由批量 LLM judge 按 0–3 分判定",
        "- 高性能请求: `stream=true`, `enable_full_content=true`, `model=non_thinking`",
        "",
        "## 核心结论数据",
        "",
        "### 检索 + 排序（A/B URL 并集为 Recall 分母）",
        "",
        "| 配置 | NDCG@10 | Recall@10 | P@10 | MRR |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ("current_source", "current_sf", "high_source", "high_baidu", "high_sf"):
        row = retrieval[name]
        lines.append(
            f"| {name} | {_fmt(row['ndcg'])} | {_fmt(row['recall'])} | "
            f"{_fmt(row['precision'])} | {_fmt(row['mrr'])} |"
        )
    lines += [
        "",
        "### 百度分数 vs SiliconFlow（完全相同的高性能候选集）",
        "",
        "| 排序 | NDCG@10 | Recall@10 | P@10 | MRR |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ("high_source", "high_baidu", "high_sf"):
        row = ranking[name]
        lines.append(
            f"| {name} | {_fmt(row['ndcg'])} | {_fmt(row['recall'])} | "
            f"{_fmt(row['precision'])} | {_fmt(row['mrr'])} |"
        )
    lines += [
        "",
        f"- 百度/SiliconFlow 分数 Spearman ρ（宏平均）：`{_fmt(summary['score_agreement']['spearman_mean'])}`",
        f"- 两种分数 Top-10 重合率（宏平均）：`{_fmt(summary['score_agreement']['top10_overlap_mean'])}`",
        f"- 高性能引用具备 rerank_score：`{summary['score_agreement']['baidu_score_coverage']}`",
        f"- 同候选集 SF−百度 NDCG@10：`{_fmt(ndcg_check['mean_delta'])}`，"
        f"query 配对 bootstrap 95% CI `[{_fmt(ndcg_check['ci95'][0])}, {_fmt(ndcg_check['ci95'][1])}]`，"
        f"胜/平/负 `{ndcg_check['wins_ties_losses']}`",
        f"- 同候选集 SF−百度 Recall@10：`{_fmt(recall_check['mean_delta'])}`，"
        f"query 配对 bootstrap 95% CI `[{_fmt(recall_check['ci95'][0])}, {_fmt(recall_check['ci95'][1])}]`，"
        f"胜/平/负 `{recall_check['wins_ties_losses']}`",
        "",
        "### 延迟",
        "",
        "| 指标 | P50 | P95 |",
        "|---|---:|---:|",
        f"| 当前搜索完成 | {_fmt(latency['current_total_p50'], 1)} ms | {_fmt(latency['current_total_p95'], 1)} ms |",
        f"| 高性能首引用 | {_fmt(latency['high_first_reference_p50'], 1)} ms | {_fmt(latency['high_first_reference_p95'], 1)} ms |",
        f"| 高性能首答案 Token | {_fmt(latency['high_first_token_p50'], 1)} ms | {_fmt(latency['high_first_token_p95'], 1)} ms |",
        f"| 高性能流完成 | {_fmt(latency['high_total_p50'], 1)} ms | {_fmt(latency['high_total_p95'], 1)} ms |",
        "",
        f"- 高性能首引用−当前搜索完成平均差："
        f"`{_fmt(checks['first_reference_minus_current_total_ms']['mean_delta'], 1)} ms`，"
        f"query 配对 bootstrap 95% CI "
        f"`[{_fmt(checks['first_reference_minus_current_total_ms']['ci95'][0], 1)}, "
        f"{_fmt(checks['first_reference_minus_current_total_ms']['ci95'][1], 1)}] ms`",
        "",
        "### enable_full_content",
        "",
        "| 接口 | 引用数 | 平均 content | 中位 content | P95 | content>snippet | content=snippet |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("current", "high_full"):
        row = content[name]
        lines.append(
            f"| {name} | {row['count']} | {_fmt(row['mean_content_chars'], 1)} | "
            f"{_fmt(row['median_content_chars'], 1)} | {_fmt(row['p95_content_chars'], 1)} | "
            f"{row['content_longer_than_snippet']} | {row['content_equals_snippet']} |"
        )
    control = content["paired_control"]
    lines += [
        "",
        f"- true/false 配对 Query：`{control['query_count']}`；匹配 URL：`{control['matched_urls']}`",
        f"- `true` 比 `false` 内容更长的匹配 URL：`{control['true_longer_count']}`",
        f"- 携带 `enable_full_content=true` 的请求成功：`{content['flag_accepted_queries']}/{summary['query_count']}`；"
        "HTTP 200 不能证明服务端实际采用该字段",
        "",
        "### 答案质量（盲化交替 A/B 位置）",
        "",
        "| 配置 | 平均总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("current_sf", "highperf"):
        row = answers[name]
        lines.append(
            f"| {name} | {_fmt(row['score'])} | {_fmt(row['correctness'])} | "
            f"{_fmt(row['completeness'])} | {_fmt(row['grounding'])} | {_fmt(row['freshness'])} |"
        )
    wins = answers["wins"]
    answer_check = checks["answer_current_minus_high_score"]
    lines += [
        "",
        f"- 胜负：highperf `{wins.get('highperf', 0)}` / current_sf `{wins.get('current_sf', 0)}` / tie `{wins.get('tie', 0)}`",
        f"- current_sf−highperf 平均答案分：`{_fmt(answer_check['mean_delta'])}`，"
        f"query 配对 bootstrap 95% CI `[{_fmt(answer_check['ci95'][0])}, {_fmt(answer_check['ci95'][1])}]`",
        "",
        "### QPS 探针",
        "",
        "| 目标发送速率 | 请求数 | 成功 | HTTP 状态 | 墙钟耗时 |",
        "|---:|---:|---:|---|---:|",
    ]
    for stage in summary["qps_probe"]:
        lines.append(
            f"| {stage['target_qps']} QPS | {stage['request_count']} | {stage['successes']} | "
            f"`{stage['statuses']}` | {_fmt(stage['wall_ms'], 1)} ms |"
        )
    lines += [
        "",
        "## 单 Query 明细",
        "",
        "| Query | 类型 | A条数 | B条数 | URL重合 | 首引用ms | B总ms | 百度NDCG | SF NDCG | 答案胜者 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for query, row in details["queries"].items():
        escaped_query = query.replace("|", "\\|")
        result = row.get("analysis") or {}
        answer_judge = row.get("answer_judge") or {}
        per_ranking = result.get("ranking_metrics_high_pool") or {}
        lines.append(
            f"| {escaped_query} | {row.get('type', '')} | "
            f"{len((row.get('current') or {}).get('references') or [])} | "
            f"{len((row.get('high_full') or {}).get('references') or [])} | "
            f"{_fmt(result.get('url_jaccard'))} | "
            f"{_fmt((row.get('high_full') or {}).get('first_reference_ms'), 1)} | "
            f"{_fmt((row.get('high_full') or {}).get('elapsed_ms'), 1)} | "
            f"{_fmt((per_ranking.get('high_baidu') or {}).get('ndcg'))} | "
            f"{_fmt((per_ranking.get('high_sf') or {}).get('ndcg'))} | "
            f"{answer_judge.get('winner', 'N/A')} |"
        )
    lines += [
        "",
        "## 解释限制",
        "",
        "- 样本仅 20 条，结果适合做接入决策，不代表全量线上分布。",
        "- relevance 与答案质量均由单一 LLM judge 评估，尚未人工复核。",
        "- current_sf 的答案由固定 Claude 证据回答器生成；当前生产搜索 API 本身不生成答案。",
        "- QPS 探针验证的是短时账号/API 接受情况，不等价于长期稳定吞吐或 SLA。",
        "- `enable_full_content` 未出现在高性能版请求参数表中；本报告只记录实际观测，不推断长期兼容性。",
        "",
    ]
    return "\n".join(lines)


def _summarize(
    details: dict[str, Any],
    rows: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    query_rows = [details["queries"][row["query"]] for row in rows]
    retrieval_names = ("current_source", "current_sf", "high_source", "high_baidu", "high_sf")
    ranking_names = ("high_source", "high_baidu", "high_sf")
    retrieval = {
        name: _mean_dict([
            row["analysis"]["retrieval_metrics_union_pool"][name]
            for row in query_rows
        ])
        for name in retrieval_names
    }
    ranking = {
        name: _mean_dict([
            row["analysis"]["ranking_metrics_high_pool"][name]
            for row in query_rows
        ])
        for name in ranking_names
    }

    statistical_checks = {
        "same_pool_sf_minus_baidu_ndcg": _paired_bootstrap([
            row["analysis"]["ranking_metrics_high_pool"]["high_sf"]["ndcg"]
            - row["analysis"]["ranking_metrics_high_pool"]["high_baidu"]["ndcg"]
            for row in query_rows
        ]),
        "same_pool_sf_minus_baidu_recall": _paired_bootstrap([
            row["analysis"]["ranking_metrics_high_pool"]["high_sf"]["recall"]
            - row["analysis"]["ranking_metrics_high_pool"]["high_baidu"]["recall"]
            for row in query_rows
        ], seed=20260724),
        "answer_current_minus_high_score": _paired_bootstrap([
            row["answer_judge"]["current_sf"]["score"]
            - row["answer_judge"]["highperf"]["score"]
            for row in query_rows
        ], seed=20260725),
        "first_reference_minus_current_total_ms": _paired_bootstrap([
            row["high_full"]["first_reference_ms"] - row["current"]["elapsed_ms"]
            for row in query_rows
        ], seed=20260726),
    }

    current_times = [row["current"]["elapsed_ms"] for row in query_rows]
    high_ref_times = [row["high_full"]["first_reference_ms"] for row in query_rows
                      if row["high_full"].get("first_reference_ms") is not None]
    high_token_times = [row["high_full"]["first_token_ms"] for row in query_rows
                        if row["high_full"].get("first_token_ms") is not None]
    high_total_times = [row["high_full"]["elapsed_ms"] for row in query_rows]

    current_stats = _content_stats([row["current"] for row in query_rows])
    high_stats = _content_stats([row["high_full"] for row in query_rows])
    matched = true_longer = 0
    for row in query_rows:
        control = row.get("high_no_full")
        if not control:
            continue
        false_map = {_ref_key(ref, i): ref for i, ref in enumerate(control.get("references") or [])}
        for index, ref in enumerate(row["high_full"].get("references") or []):
            other = false_map.get(_ref_key(ref, index))
            if other is None:
                continue
            matched += 1
            true_longer += int(len(str(ref.get("content") or "")) >
                               len(str(other.get("content") or "")))

    answer_dims = ("score", "correctness", "completeness", "grounding", "freshness")
    answer_summary = {
        name: {dim: statistics.fmean(row["answer_judge"][name][dim] for row in query_rows)
               for dim in answer_dims}
        for name in ("current_sf", "highperf")
    }
    wins: dict[str, int] = {}
    for row in query_rows:
        winner = row["answer_judge"]["winner"]
        wins[winner] = wins.get(winner, 0) + 1
    answer_summary["wins"] = wins

    spearman = [row["analysis"].get("score_spearman") for row in query_rows]
    spearman = [value for value in spearman if value is not None]
    baidu_scored = sum(
        ref.get("rerank_score") is not None
        for row in query_rows for ref in row["high_full"].get("references") or []
    )
    baidu_total = sum(len(row["high_full"].get("references") or []) for row in query_rows)

    return {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query_count": len(query_rows),
        "metric_k": args.metric_k,
        "retrieval_metrics_union_pool": retrieval,
        "ranking_metrics_high_pool": ranking,
        "statistical_checks": statistical_checks,
        "score_agreement": {
            "spearman_mean": statistics.fmean(spearman) if spearman else None,
            "top10_overlap_mean": statistics.fmean(
                row["analysis"]["baidu_sf_top10_overlap"] for row in query_rows
            ),
            "baidu_score_coverage": f"{baidu_scored}/{baidu_total}",
        },
        "latency_ms": {
            "current_total_p50": _percentile(current_times, 0.50),
            "current_total_p95": _percentile(current_times, 0.95),
            "high_first_reference_p50": _percentile(high_ref_times, 0.50),
            "high_first_reference_p95": _percentile(high_ref_times, 0.95),
            "high_first_token_p50": _percentile(high_token_times, 0.50),
            "high_first_token_p95": _percentile(high_token_times, 0.95),
            "high_total_p50": _percentile(high_total_times, 0.50),
            "high_total_p95": _percentile(high_total_times, 0.95),
        },
        "content": {
            "current": current_stats,
            "high_full": high_stats,
            "flag_accepted_queries": sum(row["high_full"].get("ok", False) for row in query_rows),
            "paired_control": {
                "query_count": sum(bool(row.get("high_no_full")) for row in query_rows),
                "matched_urls": matched,
                "true_longer_count": true_longer,
            },
        },
        "answer_quality": answer_summary,
        "qps_probe": details.get("qps_probe") or [],
    }


def _analyze_query(row: dict[str, Any], metric_k: int) -> dict[str, Any]:
    current_refs = row["current"]["references"]
    high_refs = row["high_full"]["references"]
    labels = row["relevance_labels"]
    sf_scores = row["siliconflow_scores"]
    pool = _dedup_refs((current_refs, high_refs))

    rankings = {
        "current_source": _rank_refs(current_refs, None),
        "current_sf": _rank_refs(current_refs, sf_scores),
        "high_source": _rank_refs(high_refs, None),
        "high_baidu": _rank_refs(high_refs, None, baidu=True),
        "high_sf": _rank_refs(high_refs, sf_scores),
    }
    retrieval_metrics = {
        name: _metrics_for(refs, labels, pool, metric_k)
        for name, refs in rankings.items()
    }
    ranking_metrics = {
        name: _metrics_for(rankings[name], labels, high_refs, metric_k)
        for name in ("high_source", "high_baidu", "high_sf")
    }
    current_keys = {_ref_key(ref, i) for i, ref in enumerate(current_refs)}
    high_keys = {_ref_key(ref, i) for i, ref in enumerate(high_refs)}
    union = current_keys | high_keys
    return {
        "retrieval_metrics_union_pool": retrieval_metrics,
        "ranking_metrics_high_pool": ranking_metrics,
        "url_jaccard": len(current_keys & high_keys) / len(union) if union else 0.0,
        "score_spearman": _spearman_for(high_refs, sf_scores),
        "baidu_sf_top10_overlap": _top_overlap(
            rankings["high_baidu"], rankings["high_sf"], metric_k
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="eval/dataset.jsonl")
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--metric-k", type=int, default=10)
    parser.add_argument("--full-control-queries", type=int, default=3)
    parser.add_argument("--qps-count", type=int, default=4)
    parser.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--details", default="eval/baidu_highperf_ab_details.json")
    parser.add_argument("--report", default="eval/baidu_highperf_ab_report.md")
    parser.add_argument("--skip-qps", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env()
    env = _read_project_env()
    if not settings.qianfan_api_key:
        raise ValueError("缺少 QIANFAN_API_KEY")
    if not settings.siliconflow_api_key:
        raise ValueError("缺少 SILICONFLOW_API_KEY")
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        raise ValueError("缺少 ANTHROPIC_API_KEY")

    rows = _load_queries(args.dataset, args.queries)
    details_path = Path(args.details)
    report_path = Path(args.report)
    details = _load_json(details_path, {
        "version": _CACHE_VERSION,
        "queries": {},
        "qps_probe": [],
    })
    if details.get("version") != _CACHE_VERSION:
        raise ValueError("缓存版本不匹配，请使用新的 details 路径")

    session = requests.Session()
    try:
        for index, item in enumerate(rows, 1):
            query = item["query"]
            cached = details["queries"].setdefault(query, {"type": item.get("type", "")})
            if not cached.get("current", {}).get("ok"):
                cached["current"] = _current_search(
                    session, settings.qianfan_api_key, query, args.top_k
                )
                _save_json(details_path, details)
            if not cached.get("high_full", {}).get("ok"):
                cached["high_full"] = _highperf_search(
                    session,
                    settings.qianfan_api_key,
                    query,
                    args.top_k,
                    full_content=True,
                    stream=True,
                )
                _save_json(details_path, details)
            print(
                f"[API {index:02d}/{len(rows)}] {query[:28]} "
                f"current={cached['current'].get('status')}/{len(cached['current'].get('references') or [])} "
                f"high={cached['high_full'].get('status')}/{len(cached['high_full'].get('references') or [])} "
                f"first_ref={cached['high_full'].get('first_reference_ms')}ms",
                flush=True,
            )
            if not cached["current"].get("ok") or not cached["high_full"].get("ok"):
                raise RuntimeError(f"API 请求失败，已写入缓存: {query}")

        for item in rows[: args.full_control_queries]:
            query = item["query"]
            cached = details["queries"][query]
            if not cached.get("high_no_full", {}).get("ok"):
                cached["high_no_full"] = _highperf_search(
                    session,
                    settings.qianfan_api_key,
                    query,
                    args.top_k,
                    full_content=False,
                    stream=False,
                )
                _save_json(details_path, details)
            print(
                f"[FULL CONTROL] {query[:28]} status={cached['high_no_full'].get('status')} "
                f"refs={len(cached['high_no_full'].get('references') or [])}",
                flush=True,
            )
    finally:
        session.close()

    reranker = SiliconFlowReranker(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model=settings.rerank_model,
        chunk_max_chars=settings.chunk_max_chars,
        chunk_overlap=settings.chunk_overlap,
    )
    for index, item in enumerate(rows, 1):
        query = item["query"]
        cached = details["queries"][query]
        if not cached.get("siliconflow_scores"):
            pool = _dedup_refs((cached["current"]["references"], cached["high_full"]["references"]))
            cached["siliconflow_scores"] = _score_union(reranker, query, pool)
            _save_json(details_path, details)
        print(f"[SF {index:02d}/{len(rows)}] {query[:32]}", flush=True)

    claude = ClaudeClient(
        anthropic_key,
        env.get("ANTHROPIC_BASE_URL", "").strip(),
        args.judge_model,
    )
    for index, item in enumerate(rows, 1):
        query = item["query"]
        cached = details["queries"][query]
        pool = _dedup_refs((cached["current"]["references"], cached["high_full"]["references"]))
        if not cached.get("relevance_labels"):
            cached["relevance_labels"] = _judge_relevance(claude, query, pool)
            _save_json(details_path, details)

        current_sf = _rank_refs(cached["current"]["references"], cached["siliconflow_scores"])
        if not cached.get("current_answer"):
            cached["current_answer"] = _baseline_answer(claude, query, current_sf)
            _save_json(details_path, details)
        if not cached.get("answer_judge"):
            cached["answer_judge"] = _judge_answer_pair(
                claude,
                query,
                cached["current_answer"],
                current_sf,
                cached["high_full"].get("answer") or "",
                _rank_refs(cached["high_full"]["references"], None, baidu=True),
            )
            _save_json(details_path, details)
        cached["analysis"] = _analyze_query(cached, args.metric_k)
        _save_json(details_path, details)
        print(
            f"[JUDGE {index:02d}/{len(rows)}] {query[:28]} "
            f"winner={cached['answer_judge']['winner']}",
            flush=True,
        )

    if not args.skip_qps and not details.get("qps_probe"):
        for rate in (1.0, 3.0, 5.0):
            stage = _run_qps_stage(settings.qianfan_api_key, rate, args.qps_count)
            details["qps_probe"].append(stage)
            _save_json(details_path, details)
            print(
                f"[QPS] target={rate:g} success={stage['successes']}/{stage['request_count']} "
                f"statuses={stage['statuses']}",
                flush=True,
            )
            time.sleep(2)

    summary = _summarize(details, rows, args)
    details["summary"] = summary
    _save_json(details_path, details)
    report_path.write_text(_report(summary, details, args), encoding="utf-8")
    print(f"wrote {details_path}", flush=True)
    print(f"wrote {report_path}", flush=True)


if __name__ == "__main__":
    main()
