"""LLM-as-judge:用 Claude 对 (query, 文档) 打分级相关性 0-3。

- 评分标准放 system prompt 并开启 prompt caching(降成本)。
- 判分结果落盘缓存 eval/cache/judgments.json,按 (query,url) 去重,保证可复现、省调用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

_RUBRIC = """你是搜索相关性评审。给定一个【查询】和一篇【网页内容】,判断该网页对回答查询的相关程度,按下面 4 级打分,只输出一个数字(0/1/2/3),不要任何解释:

3 = 高度相关:直接、充分地回答了查询的核心意图
2 = 相关:包含与查询直接相关的有用信息,但不够完整
1 = 略相关:沾边/提到话题但基本无法用于回答
0 = 不相关:与查询无关,或是广告/导航/无效内容

只回复 0、1、2 或 3。"""


class ClaudeJudge:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-haiku-4-5-20251001",
        cache_path: str = "eval/cache/judgments.json",
    ):
        import anthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("缺少 ANTHROPIC_API_KEY")
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
        kwargs = {"api_key": key}
        if base_url:
            kwargs["base_url"] = base_url  # 支持第三方兼容网关
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._cache: Dict[str, int] = self._load()

    def _load(self) -> Dict[str, int]:
        if os.path.exists(self.cache_path):
            with open(self.cache_path, encoding="utf-8") as f:
                return {k: v["score"] for k, v in json.load(f).items()}
        return {}

    def _save(self, raw: Dict[str, dict]) -> None:
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _key(query: str, url: str) -> str:
        return hashlib.sha1(f"{query}\n{url}".encode("utf-8")).hexdigest()

    def _ask(self, query: str, text: str) -> int:
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=8,
            system=[{"type": "text", "text": _RUBRIC,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": f"【查询】{query}\n\n【网页内容】\n{text[:2500]}"}],
        )
        out = "".join(b.text for b in msg.content if b.type == "text")
        m = re.search(r"[0-3]", out)
        return int(m.group()) if m else 0

    def score_batch(
        self, items: List[Tuple[str, str, str]], workers: int = 6
    ) -> Dict[Tuple[str, str], int]:
        """items: [(query, url, text)] -> {(query,url): score}。命中缓存的不再调用。"""
        results: Dict[Tuple[str, str], int] = {}
        todo: List[Tuple[str, str, str]] = []
        raw_full: Dict[str, dict] = {}
        if os.path.exists(self.cache_path):
            with open(self.cache_path, encoding="utf-8") as f:
                raw_full = json.load(f)

        for q, url, text in items:
            k = self._key(q, url)
            if k in self._cache:
                results[(q, url)] = self._cache[k]
            else:
                todo.append((q, url, text))

        def _work(it: Tuple[str, str, str]) -> Tuple[Tuple[str, str], int]:
            q, url, text = it
            return (q, url), self._ask(q, text)

        if todo:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for (q, url), sc in pool.map(_work, todo):
                    results[(q, url)] = sc
                    k = self._key(q, url)
                    self._cache[k] = sc
                    raw_full[k] = {"query": q, "url": url, "score": sc}
            with self._lock:
                self._save(raw_full)
        return results
