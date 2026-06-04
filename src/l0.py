"""L0 查询理解层(规则版,零额外延迟/成本)。

把用户原始查询规范化,并识别时效意图,产出结构化 SearchPlan:
  - 规范化:NFKC(全角→半角)、压缩空白、去首尾标点
  - 输入校验:空查询拦截、超长截断
  - 时效识别:关键词 → recency bucket(day/week/month/year),供 Provider
    时效过滤 + 缓存 TTL 分级
  - 路由:决定用哪些来源(当前默认全部启用源,留作未来语言路由扩展)

LLM 改写 / 子查询拆分等留待后续(需 LLM,要缓存)。
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

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


def normalize(query: str) -> str:
    """轻量规范化:全角→半角、压缩空白、去首尾标点。不改动中文正文。"""
    q = unicodedata.normalize("NFKC", query or "")
    q = re.sub(r"\s+", " ", q).strip()
    q = q.strip(" ?？。.!！,，、:：")
    return q


def detect_recency(query: str) -> str | None:
    for pat, bucket in _RECENCY_RULES:
        if pat.search(query):
            return bucket
    return None


def plan_query(query: str, providers: List[str], top_k: int = 10) -> SearchPlan:
    """规则版查询理解 → SearchPlan。"""
    norm = normalize(query)
    if not norm:
        raise ValueError("空查询")
    if len(norm) > MAX_QUERY_LEN:
        norm = norm[:MAX_QUERY_LEN]
    recency = detect_recency(norm)
    time_sensitive = recency is not None or bool(_YEAR.search(norm))
    return SearchPlan(
        raw_query=query,
        normalized_query=norm,
        recency=recency,
        time_sensitive=time_sensitive,
        providers=list(providers),
        top_k=top_k,
    )


if __name__ == "__main__":
    for q in [
        "三星堆遗址在哪个省",
        "２０２６年人工智能最新进展是什么？",
        "本周 A股 行情怎么样",
        "  请问  光合作用  的过程  ",
    ]:
        p = plan_query(q, ["tencent", "baidu"])
        print(f"{q!r}\n  -> norm={p.normalized_query!r} recency={p.recency} "
              f"time_sensitive={p.time_sensitive}\n")
