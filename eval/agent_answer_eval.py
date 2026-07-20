"""Answer-level evaluation for agent search scenarios."""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from eval.e2e_judge import compact_response, evidence_type_counts, response_fingerprint
from src.models import SearchResponse

_CACHE_DIR = "eval/cache"

_ANSWER_PROMPT = """你是技术情报 agent。给定用户任务和搜索结果证据包,请只基于这些证据回答,不要使用外部知识。

回答要求:
- 用中文输出一份紧凑但完整的技术尽调/研发情报报告
- 覆盖: 结论摘要、学术研究证据、专利布局、产业/市场信号、机会、风险、下一步建议
- 每个关键判断尽量带引用标记,例如 [web1]、[academic2]、[patent3]
- 必须先检查 retrieval_assessment.gaps 和 failures;有缺口时在对应章节明确说明,不要把 discovery evidence 当完整证据
- 如果某类证据不足,必须明确说明证据缺口,不要编造
- 如果 evidence[] 中没有 type=academic,不得把 web 文章包装成论文证据;对应章节必须写明未检索到可核验学术论文
- 如果 evidence[] 中没有 type=patent,不得把 web 文章包装成专利证据;对应章节必须写明未检索到可核验专利
- 不要输出搜索过程说明
"""

_ANSWER_PAIR_RUBRIC = """你是评测使用搜索工具的大模型最终回答质量的 judge。现在给同一个 agent 任务、两份最终回答,以及各自可核验的搜索证据摘要。

评测目标:搜索引擎好坏最终体现在 agent 能否用它的结果写出更好的答案。请把最终回答质量作为主依据,证据摘要只用于核查回答是否有来源支撑。

只根据给出的回答和证据摘要评估,不要使用外部知识补充。重点看:
- 是否完成用户任务的所有要求
- 是否正确利用学术论文/研究进展
- 是否正确利用专利、公开号、申请人或布局信息
- 是否结合产业/市场信号给出可执行判断
- 引用是否能被证据摘要支撑,是否避免编造
- 对证据不足处是否诚实说明
- 必须优先检查 support_audit:有 unsupported_refs 或 missing_*_disclosure 时,对应 grounding/research/patent 分应明显降低

请只输出 JSON,不要解释性前后缀:
{"winner":"full_agent|baidu_only|tie","full_score":0-10,"baidu_score":0-10,"full_grounding":0-2,"baidu_grounding":0-2,"full_research":0-2,"baidu_research":0-2,"full_patent":0-2,"baidu_patent":0-2,"full_synthesis":0-2,"baidu_synthesis":0-2,"reason":"不超过80字"}

score:
10 = 完整、准确、证据充分,可直接作为高质量技术尽调初稿
8 = 基本完整,少量缺口
6 = 可用但明显不均衡或证据较弱
4 = 只能回答部分问题
2 = 很弱
0 = 基本不可用或大量无依据

grounding/research/patent/synthesis:
2 = 表现好
1 = 部分可用
0 = 缺失、错误或无依据
"""


def _client(model_api_key: Optional[str] = None):
    import anthropic

    key = model_api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("缺少 ANTHROPIC_API_KEY")
    kwargs = {"api_key": key}
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def _load_json(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _text_from_message(msg: Any) -> str:
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _sha(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def _source_refs(answer: str, source: str) -> List[int]:
    pattern = re.compile(r"\[" + re.escape(source) + r"\s*(\d+)\]", re.I)
    return [int(m.group(1)) for m in pattern.finditer(answer)]


def _has_disclosure(answer: str, terms: List[str]) -> bool:
    if not any(t in answer for t in terms):
        return False
    gap_terms = ["不足", "缺少", "缺失", "未检索", "没有", "无法", "缺口", "空缺"]
    return any(t in answer for t in gap_terms)


def answer_support_audit(answer: str, resp: SearchResponse) -> dict:
    counts = evidence_type_counts(resp)
    refs = {source: _source_refs(answer, source) for source in counts}
    unsupported = []
    for source, nums in refs.items():
        max_n = counts[source]
        for n in nums:
            if n < 1 or n > max_n:
                unsupported.append(f"{source}{n}")

    talks_academic = any(t in answer for t in ["学术", "论文", "文献", "研究进展"])
    talks_patent = any(t in answer for t in ["专利", "公开号", "申请人", "布局"])
    missing_academic = (
        counts["academic"] == 0
        and talks_academic
        and not _has_disclosure(answer, ["学术", "论文", "文献", "研究进展"])
    )
    missing_patent = (
        counts["patent"] == 0
        and talks_patent
        and not _has_disclosure(answer, ["专利", "公开号", "申请人", "布局"])
    )
    return {
        "result_counts": counts,
        "refs": refs,
        "unsupported_refs": unsupported,
        "missing_academic_disclosure": missing_academic,
        "missing_patent_disclosure": missing_patent,
        "flag_count": len(unsupported) + int(missing_academic) + int(missing_patent),
    }


class EvidenceAnswerAgent:
    """Generate final agent answers from a fixed search result bundle."""

    def __init__(
        self,
        model: str,
        evidence_k: int = 8,
        cache_path: str = os.path.join(_CACHE_DIR, "agent_answers.json"),
        api_key: Optional[str] = None,
    ) -> None:
        self.client = _client(api_key)
        self.model = model
        self.evidence_k = evidence_k
        self.cache_path = cache_path
        self.cache = _load_json(cache_path)

    def _key(self, task: str, engine_name: str, resp: SearchResponse) -> str:
        return _sha({
            "prompt_version": 2,
            "model": self.model,
            "task": task,
            "engine": engine_name,
            "evidence_k": self.evidence_k,
            "response": response_fingerprint(resp),
        })

    def generate_batch(
        self,
        items: List[Tuple[str, str, str, SearchResponse]],
        workers: int = 4,
    ) -> Dict[str, str]:
        out: Dict[str, str] = {}
        todo: List[Tuple[str, str, str, SearchResponse, str]] = []
        for sid, engine_name, task, resp in items:
            key = self._key(task, engine_name, resp)
            out[f"{sid}:{engine_name}"] = self.cache.get(key, "")
            if key not in self.cache:
                todo.append((sid, engine_name, task, resp, key))

        if todo:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for sid, engine_name, key, answer in pool.map(self._generate_one, todo):
                    self.cache[key] = answer
                    out[f"{sid}:{engine_name}"] = answer
            _save_json(self.cache_path, self.cache)
        return out

    def _generate_one(
        self,
        item: Tuple[str, str, str, SearchResponse, str],
    ) -> Tuple[str, str, str, str]:
        sid, engine_name, task, resp, key = item
        payload = {
            "task": task,
            "engine": engine_name,
            "evidence": compact_response(resp, per_block_k=self.evidence_k),
        }
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=1800,
            system=[{"type": "text", "text": _ANSWER_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        return sid, engine_name, key, _text_from_message(msg)


class AnswerPairJudge:
    """Compare final answers generated from two search engines."""

    def __init__(
        self,
        model: str,
        evidence_k: int = 8,
        cache_path: str = os.path.join(_CACHE_DIR, "agent_answer_pair_judgments.json"),
        api_key: Optional[str] = None,
    ) -> None:
        self.client = _client(api_key)
        self.model = model
        self.evidence_k = evidence_k
        self.cache_path = cache_path
        self.cache = _load_json(cache_path)

    def _key(
        self,
        task: str,
        full: SearchResponse,
        full_answer: str,
        baidu: SearchResponse,
        baidu_answer: str,
    ) -> str:
        return _sha({
            "rubric_version": 2,
            "model": self.model,
            "task": task,
            "evidence_k": self.evidence_k,
            "full_response": response_fingerprint(full),
            "baidu_response": response_fingerprint(baidu),
            "full_answer": hashlib.sha1(full_answer.encode("utf-8")).hexdigest(),
            "baidu_answer": hashlib.sha1(baidu_answer.encode("utf-8")).hexdigest(),
        })

    def score_batch(
        self,
        items: List[Tuple[str, str, SearchResponse, str, SearchResponse, str]],
        workers: int = 4,
    ) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        todo: List[Tuple[str, str, SearchResponse, str, SearchResponse, str, str]] = []
        for sid, task, full, full_answer, baidu, baidu_answer in items:
            key = self._key(task, full, full_answer, baidu, baidu_answer)
            if key in self.cache:
                out[sid] = self.cache[key]
            else:
                todo.append((sid, task, full, full_answer, baidu, baidu_answer, key))

        if todo:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for sid, key, judgment in pool.map(self._score_one, todo):
                    self.cache[key] = judgment
                    out[sid] = judgment
            _save_json(self.cache_path, self.cache)
        return out

    def _score_one(
        self,
        item: Tuple[str, str, SearchResponse, str, SearchResponse, str, str],
    ) -> Tuple[str, str, dict]:
        sid, task, full, full_answer, baidu, baidu_answer, key = item
        payload = {
            "task": task,
            "full_agent": {
                "answer": full_answer,
                "evidence": compact_response(full, per_block_k=self.evidence_k),
                "support_audit": answer_support_audit(full_answer, full),
            },
            "baidu_only": {
                "answer": baidu_answer,
                "evidence": compact_response(baidu, per_block_k=self.evidence_k),
                "support_audit": answer_support_audit(baidu_answer, baidu),
            },
        }
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=[{"type": "text", "text": _ANSWER_PAIR_RUBRIC,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        return sid, key, _parse_answer_pair(_text_from_message(msg))


def _parse_answer_pair(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return _answer_pair_fallback("judge输出无法解析")
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return _answer_pair_fallback("judge输出非JSON")

    winner = str(data.get("winner", "tie"))
    if winner not in ("full_agent", "baidu_only", "tie"):
        winner = "tie"
    return {
        "winner": winner,
        "full_score": _clamp_int(data.get("full_score"), 0, 10),
        "baidu_score": _clamp_int(data.get("baidu_score"), 0, 10),
        "full_grounding": _clamp_int(data.get("full_grounding"), 0, 2),
        "baidu_grounding": _clamp_int(data.get("baidu_grounding"), 0, 2),
        "full_research": _clamp_int(data.get("full_research"), 0, 2),
        "baidu_research": _clamp_int(data.get("baidu_research"), 0, 2),
        "full_patent": _clamp_int(data.get("full_patent"), 0, 2),
        "baidu_patent": _clamp_int(data.get("baidu_patent"), 0, 2),
        "full_synthesis": _clamp_int(data.get("full_synthesis"), 0, 2),
        "baidu_synthesis": _clamp_int(data.get("baidu_synthesis"), 0, 2),
        "reason": str(data.get("reason", ""))[:120],
    }


def _answer_pair_fallback(reason: str) -> dict:
    return {
        "winner": "tie",
        "full_score": 0,
        "baidu_score": 0,
        "full_grounding": 0,
        "baidu_grounding": 0,
        "full_research": 0,
        "baidu_research": 0,
        "full_patent": 0,
        "baidu_patent": 0,
        "full_synthesis": 0,
        "baidu_synthesis": 0,
        "reason": reason,
    }
