"""SiliconFlow query rewrite adapter and legacy function wrappers."""
from __future__ import annotations

from threading import Lock
from typing import Any, List, Optional

import requests

from src.application.ports.cache import CacheBackend
from src.domain.errors import ExternalServiceError
from src.infrastructure.cache import InMemoryCache
from src.infrastructure.http_errors import external_http_error
from src.domain.failures import SearchFailure

_REWRITE_PROMPT = """你是一个搜索查询优化器。将用户查询改写为更适合搜索引擎的简洁关键词。

规则:
- 保留原始语义,不要添加或歪曲信息
- 去掉口语化表达和冗余修饰语
- 保留时间信息
- 输出简洁的搜索关键词,不要解释,不要加引号
- 保持查询语言
- 只输出改写后的查询"""

_ACADEMIC_REWRITE_PROMPT = """你是学术检索查询优化器。从用户问题中提取用于学术论文数据库检索的核心查询。

规则:
- 若问题中已包含论文标题,直接输出该论文标题
- 否则提取核心学术术语,优先翻译为英文
- 去掉疑问词与修饰
- 只输出检索词本身,不要解释、引号或句末标点"""


class SiliconFlowQueryRewriter:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        cache: CacheBackend,
        http_session: Any = None,
        cache_ttl: int = 3600,
        timeout: int = 5,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._cache = cache
        self._http = http_session or requests
        self._cache_ttl = cache_ttl
        self._timeout = timeout

    def rewrite(self, query: str, *, academic: bool = False) -> str:
        mode = "academic" if academic else "general"
        key = f"rewrite:{self._model}:{mode}:{query}"
        cached = self._cache.get(key)
        if cached is not None:
            if not isinstance(cached, str):
                raise TypeError("rewrite cache value must be str")
            return cached
        prompt = _ACADEMIC_REWRITE_PROMPT if academic else _REWRITE_PROMPT
        try:
            response = self._http.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": query},
                    ],
                    "max_tokens": 64 if academic else 128,
                    "temperature": 0.0 if academic else 0.1,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            rewritten = response.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            code = "ACADEMIC_QUERY_REWRITE_FAILED" if academic else "QUERY_REWRITE_FAILED"
            raise external_http_error("siliconflow", code.lower(), exc) from exc
        rewritten = rewritten.strip('"\'“”‘’').strip()
        if not rewritten or (not academic and len(rewritten) >= len(query) * 3):
            return query
        self._cache.set(key, rewritten, self._cache_ttl)
        return rewritten


_legacy_lock = Lock()
_legacy_rewriters: dict[tuple[int, str, str, str], SiliconFlowQueryRewriter] = {}


def _legacy_rewriter(
    api_key: str,
    base_url: str,
    model: str,
    cache_size: int,
    http_session: Any,
) -> SiliconFlowQueryRewriter:
    key = (cache_size, api_key, base_url, model)
    with _legacy_lock:
        rewriter = _legacy_rewriters.get(key)
        if rewriter is None or http_session is not None:
            rewriter = SiliconFlowQueryRewriter(
                api_key,
                base_url,
                model,
                cache=InMemoryCache(cache_size),
                http_session=http_session,
            )
            if http_session is None:
                _legacy_rewriters[key] = rewriter
        return rewriter


def _legacy_rewrite(
    query: str,
    api_key: str,
    base_url: str,
    model: str,
    cache_size: int,
    failures: Optional[List[SearchFailure]],
    http_session: Any,
    *,
    academic: bool,
) -> str:
    try:
        return _legacy_rewriter(
            api_key, base_url, model, cache_size, http_session
        ).rewrite(query, academic=academic)
    except ExternalServiceError as exc:
        code = "ACADEMIC_QUERY_REWRITE_FAILED" if academic else "QUERY_REWRITE_FAILED"
        if failures is not None:
            failures.append(SearchFailure(
                stage="academic_query_rewrite" if academic else "query_rewrite",
                source="siliconflow",
                type="academic" if academic else None,
                code=code,
                message=str(exc),
            ))
        print(f"[l0] 查询改写失败,使用原查询: code={code}")
        return query


def rewrite_query(
    query: str,
    api_key: str,
    base_url: str = "https://api.siliconflow.cn/v1",
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    cache_size: int = 512,
    failures: Optional[List[SearchFailure]] = None,
    http_session: Any = None,
) -> str:
    return _legacy_rewrite(
        query,
        api_key,
        base_url,
        model,
        cache_size,
        failures,
        http_session,
        academic=False,
    )


def rewrite_academic_query(
    query: str,
    api_key: str,
    base_url: str = "https://api.siliconflow.cn/v1",
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    cache_size: int = 512,
    failures: Optional[List[SearchFailure]] = None,
    http_session: Any = None,
) -> str:
    return _legacy_rewrite(
        query,
        api_key,
        base_url,
        model,
        cache_size,
        failures,
        http_session,
        academic=True,
    )
