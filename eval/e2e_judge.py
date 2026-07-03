"""LLM judge for end-to-end search response bundles."""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from src.models import AcademicResult, PatentResult, SearchResponse, SearchResult

_CACHE_DIR = "eval/cache"

_BUNDLE_RUBRIC = """你是 Agent 搜索引擎的端到端评测员。给定【用户查询】和搜索 API 返回的结果包,判断这些结果整体是否足以支持一个 agent 准确回答用户问题。

只评估返回结果包本身,不要使用外部知识补答案。关注:
- 结果是否直接覆盖用户意图
- 学术/专利查询是否出现对应的论文/专利证据
- 证据是否具体、权威、可引用,而不是泛泛网页或噪声
- 对时效问题,结果是否看起来足够新

请只输出 JSON,不要解释性前后缀:
{"score":0-4,"source_fit":0-2,"evidence":0-2,"reason":"不超过40字"}

score:
4 = 结果包足以高质量回答,核心证据完整且排序合理
3 = 基本足以回答,但有少量缺口或噪声
2 = 只能部分回答,需要额外搜索补充
1 = 相关性很弱,大多不能支撑回答
0 = 基本不可用或与查询无关

source_fit:
2 = 使用了合适结果类型(web/academic/patent),没有明显误触发
1 = 结果类型大致可用,但缺少应有垂直源或有明显混杂
0 = 结果类型错误,例如专利问题没有专利、论文问题没有论文

evidence:
2 = 标题/摘要/元数据足以形成可靠证据链
1 = 有一些证据,但太泛或元数据不足
0 = 缺少可用证据
"""

_PAIRWISE_RUBRIC = """你是使用搜索工具的大模型评测员。现在要比较两个搜索引擎给同一个 agent 任务返回的证据包:
- full_agent: 可返回 web、academic_results、patent_results
- baidu_only: 只返回百度 web 结果

任务通常是技术尽调/R&D 情报,要求大模型综合:
1. 学术研究进展或关键论文
2. 专利布局、申请人或公开号
3. 产业/市场/公司信号
4. 可执行的机会、风险或下一步建议

只根据给出的证据包评估,不要使用外部知识补充。判断哪个结果包更适合交给大模型生成最终报告。

请只输出 JSON,不要解释性前后缀:
{"winner":"full_agent|baidu_only|tie","full_score":0-5,"baidu_score":0-5,"research":0-2,"patent":0-2,"synthesis":0-2,"reason":"不超过60字"}

分数含义:
5 = 能高质量支撑完整技术尽调
4 = 基本完整,少量缺口
3 = 可用但证据不均衡
2 = 只能回答一部分
1 = 很弱
0 = 基本不可用

research/patent/synthesis 分别评 full_agent 相对 baidu_only 的优势:
2 = full_agent 明显更好
1 = 略好或各有优劣
0 = 无优势或更差
"""


def _result_summary(result: SearchResult) -> dict:
    text = result.content or result.snippet or ""
    item = {
        "title": result.title[:180],
        "url": result.url,
        "source": result.source,
        "site": result.site,
        "date": result.date,
        "snippet": text[:700],
    }
    if isinstance(result, AcademicResult):
        item.update({
            "year": result.year,
            "venue": result.venue,
            "citations": result.citations,
            "doi": result.doi,
            "is_oa": result.is_oa,
        })
    if isinstance(result, PatentResult):
        item.update({
            "publication_number": result.publication_number,
            "applicant": result.applicant[:4],
            "application_date": result.application_date,
            "publication_date": result.publication_date,
            "country": result.country,
            "ipc_main": result.ipc_main,
            "cpc_main": result.cpc_main,
        })
    return item


def compact_response(resp: SearchResponse, per_block_k: int = 5) -> dict:
    return {
        "normalized_query": resp.normalized_query,
        "rewritten_query": resp.rewritten_query,
        "time_sensitive": resp.time_sensitive,
        "recency": resp.recency,
        "providers_used": resp.providers_used,
        "reranker": resp.reranker,
        "elapsed_ms": resp.elapsed_ms,
        "web_results": [_result_summary(r) for r in resp.results[:per_block_k]],
        "academic_query": resp.academic_query,
        "academic_results": [_result_summary(r) for r in resp.academic_results[:per_block_k]],
        "patent_query": resp.patent_query,
        "patent_results": [_result_summary(r) for r in resp.patent_results[:per_block_k]],
    }


def response_fingerprint(resp: SearchResponse) -> str:
    raw = json.dumps(compact_response(resp, per_block_k=10), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class E2EBundleJudge:
    def __init__(
        self,
        model: str,
        cache_path: str = os.path.join(_CACHE_DIR, "e2e_bundle_judgments.json"),
        api_key: Optional[str] = None,
    ) -> None:
        import anthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("缺少 ANTHROPIC_API_KEY")
        kwargs = {"api_key": key}
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.cache_path = cache_path
        self.cache = self._load()

    def _load(self) -> Dict[str, dict]:
        if os.path.exists(self.cache_path):
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _key(query: str, fingerprint: str) -> str:
        return hashlib.sha1(f"{query}\n{fingerprint}".encode("utf-8")).hexdigest()

    def score_batch(
        self, items: List[Tuple[str, SearchResponse]], workers: int = 4
    ) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        todo: List[Tuple[str, SearchResponse, str]] = []
        for query, resp in items:
            fp = response_fingerprint(resp)
            key = self._key(query, fp)
            if key in self.cache:
                out[query] = self.cache[key]
            else:
                todo.append((query, resp, key))

        if todo:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for query, key, judgment in pool.map(self._score_one, todo):
                    self.cache[key] = judgment
                    out[query] = judgment
            self._save()
        return out

    def _score_one(self, item: Tuple[str, SearchResponse, str]) -> Tuple[str, str, dict]:
        query, resp, key = item
        payload = {"query": query, "response": compact_response(resp)}
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=256,
            system=[{"type": "text", "text": _BUNDLE_RUBRIC,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return query, key, _parse_judgment(text)


def _parse_judgment(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {"score": 0, "source_fit": 0, "evidence": 0, "reason": "judge输出无法解析"}
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return {"score": 0, "source_fit": 0, "evidence": 0, "reason": "judge输出非JSON"}
    return {
        "score": _clamp_int(data.get("score"), 0, 4),
        "source_fit": _clamp_int(data.get("source_fit"), 0, 2),
        "evidence": _clamp_int(data.get("evidence"), 0, 2),
        "reason": str(data.get("reason", ""))[:80],
    }


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def quality_value(judgment: dict) -> float:
    return (
        0.70 * (judgment["score"] / 4)
        + 0.15 * (judgment["source_fit"] / 2)
        + 0.15 * (judgment["evidence"] / 2)
    )


class ScenarioPairJudge:
    def __init__(
        self,
        model: str,
        cache_path: str = os.path.join(_CACHE_DIR, "agent_scenario_pair_judgments.json"),
        api_key: Optional[str] = None,
    ) -> None:
        import anthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("缺少 ANTHROPIC_API_KEY")
        kwargs = {"api_key": key}
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.cache_path = cache_path
        self.cache = self._load()

    def _load(self) -> Dict[str, dict]:
        if os.path.exists(self.cache_path):
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _key(task: str, full: SearchResponse, baidu: SearchResponse) -> str:
        raw = json.dumps(
            {
                "task": task,
                "full": response_fingerprint(full),
                "baidu": response_fingerprint(baidu),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def score_batch(
        self, items: List[Tuple[str, SearchResponse, SearchResponse]], workers: int = 4
    ) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        todo: List[Tuple[str, SearchResponse, SearchResponse, str]] = []
        for task, full, baidu in items:
            key = self._key(task, full, baidu)
            if key in self.cache:
                out[task] = self.cache[key]
            else:
                todo.append((task, full, baidu, key))

        if todo:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for task, key, judgment in pool.map(self._score_one, todo):
                    self.cache[key] = judgment
                    out[task] = judgment
            self._save()
        return out

    def _score_one(
        self, item: Tuple[str, SearchResponse, SearchResponse, str]
    ) -> Tuple[str, str, dict]:
        task, full, baidu, key = item
        payload = {
            "task": task,
            "full_agent": compact_response(full, per_block_k=6),
            "baidu_only": compact_response(baidu, per_block_k=12),
        }
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=256,
            system=[{"type": "text", "text": _PAIRWISE_RUBRIC,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return task, key, _parse_pairwise(text)


def _parse_pairwise(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return _pair_fallback("judge输出无法解析")
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return _pair_fallback("judge输出非JSON")

    winner = str(data.get("winner", "tie"))
    if winner not in ("full_agent", "baidu_only", "tie"):
        winner = "tie"
    return {
        "winner": winner,
        "full_score": _clamp_int(data.get("full_score"), 0, 5),
        "baidu_score": _clamp_int(data.get("baidu_score"), 0, 5),
        "research": _clamp_int(data.get("research"), 0, 2),
        "patent": _clamp_int(data.get("patent"), 0, 2),
        "synthesis": _clamp_int(data.get("synthesis"), 0, 2),
        "reason": str(data.get("reason", ""))[:100],
    }


def _pair_fallback(reason: str) -> dict:
    return {
        "winner": "tie",
        "full_score": 0,
        "baidu_score": 0,
        "research": 0,
        "patent": 0,
        "synthesis": 0,
        "reason": reason,
    }
