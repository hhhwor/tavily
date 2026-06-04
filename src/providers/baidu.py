"""百度千帆 AI 搜索 Provider (web_search)。

接口文档: https://cloud.baidu.com/doc/qianfan-api/s/em82g4tlk
  - Endpoint: POST https://qianfan.baidubce.com/v2/ai_search/web_search
  - 鉴权:     Authorization: Bearer <QIANFAN_API_KEY>
  - 限制:     查询仅单轮、<=72 字符(汉字算 2 字符),超出截断。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from src.models import SearchResult
from src.providers.base import SearchProvider

_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
_QUERY_LIMIT = 72  # 字符配额:汉字算 2

# 超长时优先剥离的口语化前缀(保守:只去明显冗余,不动核心语义)
_FILLER_PREFIXES = (
    "我想知道", "我想了解一下", "我想了解", "我想问一下", "我想问", "请问一下",
    "请问", "帮我查一下", "帮我查", "帮我搜", "帮我", "谁能告诉我", "麻烦", "想了解",
)


def _width(s: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in s)


def trim_query(query: str, limit: int = _QUERY_LIMIT) -> str:
    """按百度规则裁剪到 <=limit 字符配额(汉字算 2)。

    先剥离口语化前缀,再按配额硬截断,尽量保留核心语义。
    """
    q = query.strip()
    if _width(q) <= limit:
        return q
    for f in _FILLER_PREFIXES:
        if q.startswith(f):
            q = q[len(f):].strip()
            break
    if _width(q) <= limit:
        return q
    out: List[str] = []
    used = 0
    for ch in q:
        w = 2 if ord(ch) > 127 else 1
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out)


class BaiduSearchProvider(SearchProvider):
    name = "baidu"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 15):
        self.api_key = api_key or os.getenv("QIANFAN_API_KEY", "")
        self.timeout = timeout
        if not self.api_key:
            raise ValueError("缺少百度千帆凭证: QIANFAN_API_KEY")

    def search(self, query: str, top_k: int = 10, recency: Optional[str] = None) -> List[SearchResult]:
        body: Dict[str, Any] = {
            "messages": [{"role": "user", "content": trim_query(query)}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": min(max(top_k, 1), 50)}],
        }
        # 时效过滤:recency bucket → search_recency_filter(枚举 week/month/year)
        if recency:
            body["search_recency_filter"] = {"day": "week", "week": "week",
                                             "month": "month", "year": "year"}.get(recency, "month")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(_ENDPOINT, headers=headers, json=body, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if "references" not in data:
            msg = data.get("message") or data.get("msg") or data
            raise RuntimeError(f"百度搜索错误: {msg}")
        return self._normalize(data["references"])[:top_k]

    def _normalize(self, refs: List[Dict[str, Any]]) -> List[SearchResult]:
        results: List[SearchResult] = []
        for r in refs:
            body = r.get("content", "") or ""
            results.append(
                SearchResult(
                    url=r.get("url", ""),
                    title=r.get("title", ""),
                    snippet=r.get("snippet", "") or body[:200],
                    content=body,
                    date=r.get("date", "") or "",
                    site=r.get("website", "") or "",
                    score=None,
                    source=self.name,
                    raw=r,
                )
            )
        return results
