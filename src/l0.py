"""L0 查询理解层。

规则版(零延迟/成本):NFKC 规范化 + 时效识别 + 输入校验
LLM 改写(可选):口语化→检索关键词,缓存命中时零延迟

产出 SearchPlan,引擎据此执行检索。
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections import OrderedDict
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests as _requests

from src.models import SearchPlan

MAX_QUERY_LEN = 512  # 边界校验:超长截断,防滥用

# 时效关键词 → bucket(顺序敏感:先具体后泛化)
_RECENCY_RULES = [
    (re.compile(r"今天|今日|\btoday\b", re.I), "day"),
    (re.compile(r"本周|这周|近.{0,2}周|过去.{0,2}周|最近几天|this week|past week", re.I), "week"),
    (re.compile(r"本月|近.{0,2}个?月|最近.{0,2}个?月|this month|past month", re.I), "month"),
    (re.compile(r"今年|近.{0,2}年|过去.{0,2}年|this year", re.I), "year"),
    # 泛化的"最新/近期"无明确窗口 → 默认 month(平衡新鲜度与召回)
    (re.compile(r"最新|最近|近期|实时|latest|recent|newest", re.I), "month"),
]
_YEAR = re.compile(r"\b20\d{2}\b")

# 学术意图关键词(高精度:英文用词边界避免误匹配,如 \bpaper 不命中 newspaper)
_ACADEMIC_RULES = re.compile(
    r"论文|文献|综述|预印本|期刊|学术|被引|引文|研究综述|发表|"
    r"\barxiv\b|\bpapers?\b|\bpreprints?\b|\bsurvey\b|\bliterature\b|"
    r"\bcitations?\b|\bcited by\b|\bpeer.?reviewed\b|\bet al\.?|\bdoi\b|"
    r"\bpubmed\b|\bscholar\b|\bjournals?\b|\bproceedings\b|\bbibliograph",
    re.I,
)

# 专利意图关键词(中文专利库;英文用词边界避免误匹配)
_PATENT_RULES = re.compile(
    r"专利|发明专利|实用新型|外观设计|专利申请|专利号|公开号|申请号|授权号|"
    r"权利要求|权要|申请人|发明人|"
    r"\bpatents?\b|\bpatented\b|\bpatentability\b|\binvention\b|\bIPC\b|"
    r"\bUSPTO\b|\bWIPO\b|\bEPO\b",
    re.I,
)

# LLM 改写 prompt
_REWRITE_PROMPT = """你是一个搜索查询优化器。将用户查询改写为更适合搜索引擎的简洁关键词。

规则:
- 保留原始语义,不要添加或歪曲信息
- 去掉口语化表达(请问、我想知道、帮我查一下、怎么样)
- 去掉冗余的语气词和修饰语
- 保留时间信息(今天、本周、2026年)
- 输出简洁的搜索关键词,不要解释,不要加引号
- 保持查询语言(中文→中文,英文→英文)
- 只输出改写后的查询,不要输出其他任何内容"""


class _LRUCache:
    """简单的 LRU 缓存,带 TTL。"""

    def __init__(self, capacity: int = 512, ttl: int = 3600):
        self._cache: OrderedDict = OrderedDict()
        self._ttl = ttl

    def get(self, key: str) -> Optional[str]:
        if key in self._cache:
            value, ts = self._cache[key]
            if time.time() - ts < self._ttl:
                self._cache.move_to_end(key)
                return value
            del self._cache[key]
        return None

    def put(self, key: str, value: str) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self._cache_capacity:
            self._cache.popitem(last=False)
        self._cache[key] = (value, time.time())

    @property
    def _cache_capacity(self) -> int:
        return 512  # 会被 __init__ 覆盖

    def __len__(self) -> int:
        return len(self._cache)


class _RewriteCache:
    """LRU 缓存,按容量初始化。"""

    def __init__(self, capacity: int = 512, ttl: int = 3600):
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._capacity = capacity
        self._ttl = ttl

    def get(self, key: str) -> Optional[str]:
        if key in self._data:
            value, ts = self._data[key]
            if time.time() - ts < self._ttl:
                self._data.move_to_end(key)
                return value
            del self._data[key]
        return None

    def put(self, key: str, value: str) -> None:
        if key in self._data:
            del self._data[key]
        elif len(self._data) >= self._capacity:
            self._data.popitem(last=False)
        self._data[key] = (value, time.time())


_rewrite_cache: Optional[_RewriteCache] = None


def _get_cache(capacity: int = 512) -> _RewriteCache:
    global _rewrite_cache
    if _rewrite_cache is None:
        _rewrite_cache = _RewriteCache(capacity=capacity)
    return _rewrite_cache


def normalize(query: str) -> str:
    """轻量规范化:全角→半角、压缩空白、去首尾标点。不改动中文正文。"""
    q = unicodedata.normalize("NFKC", query or "")
    q = re.sub(r"\s+", " ", q).strip()
    q = q.strip(" ?？。.!！,，、:：")
    return q


def detect_recency(query: str) -> Optional[str]:
    for pat, bucket in _RECENCY_RULES:
        if pat.search(query):
            return bucket
    return None


def detect_academic(query: str) -> bool:
    """识别学术检索意图(论文/文献/综述/arxiv/citation 等)。"""
    return bool(_ACADEMIC_RULES.search(query))


def detect_patent(query: str) -> bool:
    """识别专利检索意图(专利/发明/实用新型/公开号/patent 等)。"""
    return bool(_PATENT_RULES.search(query))


def rewrite_query(
    query: str,
    api_key: str,
    base_url: str = "https://api.siliconflow.cn/v1",
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    cache_size: int = 512,
) -> str:
    """用 LLM 将查询改写为检索友好的关键词。

    失败时回退到原始 query,保证可用性。
    """
    cache = _get_cache(cache_size)
    cached = cache.get(query)
    if cached is not None:
        return cached

    try:
        resp = _requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _REWRITE_PROMPT},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 128,
                "temperature": 0.1,
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        rewritten = data["choices"][0]["message"]["content"].strip()
        # 清理:去引号、去首尾空白
        rewritten = rewritten.strip('"\'""''').strip()
        if rewritten and len(rewritten) < len(query) * 3:
            cache.put(query, rewritten)
            return rewritten
    except Exception as e:
        print(f"[l0] 查询改写失败,使用原始查询: {e}")

    return query


_ACADEMIC_REWRITE_PROMPT = """你是学术检索查询优化器。从用户问题中提取用于学术论文数据库(OpenAlex,以英文文献为主)检索的核心查询。

规则:
- 若问题中已包含论文标题(通常是英文),直接输出该论文标题,不要改动
- 否则提取核心学术术语/概念,优先翻译为英文(如「生成对抗网络」→「Generative Adversarial Networks」)
- 去掉所有疑问词与修饰(如「的第一作者是谁」「是什么」「总参数量是多少」「当时在哪家公司」)
- 只输出检索词本身,不要解释、不要引号、不要句末标点"""


def rewrite_academic_query(
    query: str,
    api_key: str,
    base_url: str = "https://api.siliconflow.cn/v1",
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    cache_size: int = 512,
) -> str:
    """把(可能中文/口语化的)查询改写为适合 OpenAlex 的学术检索词。

    解决「英文标题 + 中文疑问句」混合 query 在 OpenAlex 召回为空的问题。
    失败回退原查询;结果按 'acad::'+query 缓存,与普通改写区分。
    """
    cache = _get_cache(cache_size)
    ck = "acad::" + query
    cached = cache.get(ck)
    if cached is not None:
        return cached

    try:
        resp = _requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _ACADEMIC_REWRITE_PROMPT},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 64,
                "temperature": 0.0,
            },
            timeout=5,
        )
        resp.raise_for_status()
        rewritten = resp.json()["choices"][0]["message"]["content"].strip()
        rewritten = rewritten.strip('"\'""''').strip()
        if rewritten:
            cache.put(ck, rewritten)
            return rewritten
    except Exception as e:
        print(f"[l0] 学术查询改写失败,用原查询: {e}")

    return query


def plan_query(
    query: str,
    providers: List[str],
    top_k: int = 10,
    rewrite: bool = False,
    rewrite_api_key: str = "",
    rewrite_base_url: str = "https://api.siliconflow.cn/v1",
    rewrite_model: str = "Qwen/Qwen2.5-7B-Instruct",
    rewrite_cache_size: int = 512,
    academic_detect: bool = True,
    force_academic: Optional[bool] = None,
    patent_detect: bool = True,
    force_patent: Optional[bool] = None,
) -> SearchPlan:
    """L0 查询理解:规范化 + 时效识别 + 学术/专利意图识别 + (可选)LLM 改写 → SearchPlan。

    force_academic / force_patent: None=按对应 detect 自动识别;True/False=显式覆盖。
    """
    norm = normalize(query)
    if not norm:
        raise ValueError("空查询")
    if len(norm) > MAX_QUERY_LEN:
        norm = norm[:MAX_QUERY_LEN]
    recency = detect_recency(norm)
    time_sensitive = recency is not None or bool(_YEAR.search(norm))
    if force_academic is not None:
        academic = force_academic
    else:
        academic = academic_detect and detect_academic(norm)
    if force_patent is not None:
        patent = force_patent
    else:
        patent = patent_detect and detect_patent(norm)

    rewritten = None
    if rewrite and rewrite_api_key:
        rewritten = rewrite_query(
            norm, rewrite_api_key, rewrite_base_url, rewrite_model, rewrite_cache_size
        )

    return SearchPlan(
        raw_query=query,
        normalized_query=norm,
        rewritten_query=rewritten,
        recency=recency,
        time_sensitive=time_sensitive,
        academic=academic,
        patent=patent,
        providers=list(providers),
        top_k=top_k,
    )


if __name__ == "__main__":
    for q in [
        "三星堆遗址在哪个省",
        "２０２６年人工智能最新进展是什么？",
        "本周 A股 行情怎么样",
        "  请问  光合作用  的过程  ",
        "我想知道最近AI有什么新进展",
    ]:
        p = plan_query(q, ["tencent", "baidu"])
        print(
            f"{q!r}\n  -> norm={p.normalized_query!r} recency={p.recency} "
            f"time_sensitive={p.time_sensitive}\n"
        )
