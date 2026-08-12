"""SiliconFlow Qwen3 structured intent-classification adapter."""
from __future__ import annotations

import json
from typing import Any

import requests

from src.application.ports.cache import CacheBackend
from src.domain.errors import ExternalServiceError
from src.domain.intent import IntentDecision
from src.infrastructure.http_errors import external_http_error
from src.infrastructure.http_timeout import bounded_http_timeout


_INTENT_PROMPT = """你是检索系统的意图路由器。仅输出一个 JSON 对象，不要 Markdown 或解释。

字段：
- intent：且只能为 general_search、legal、academic_literature、patent、mixed_research 之一。
- source_types：数组，元素只能为 academic、patent、legal。general_search 必须为 []；legal 必须为 ["legal"]；academic_literature 必须为 ["academic"]；patent 必须为 ["patent"]；mixed_research 必须包含至少两个不同元素。
- confidence：0 到 1 的数字。
- legal_mode：exact_citation、interpretation、general 或 null；只有 source_types 包含 legal 时才可非 null。

规则：法律法规、法条、司法解释、法条效力等选择 legal；论文、文献、DOI、综述、学术研究选择 academic_literature；专利、IPC、申请号、权利要求选择 patent；明确要求两个或以上垂类时选择 mixed_research；其他检索选择 general_search。"""


class SiliconFlowIntentClassifier:
    """Call Qwen3 with JSON mode and validate every response before routing."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        cache: CacheBackend,
        http_session: Any = None,
        cache_ttl: int = 3600,
        timeout: int = 8,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._cache = cache
        self._http = http_session or requests
        self._cache_ttl = cache_ttl
        self._timeout = timeout

    def classify(self, query: str) -> IntentDecision:
        return self.classify_with_timeout(query)

    def classify_with_timeout(
        self,
        query: str,
        *,
        timeout_seconds: float | None = None,
    ) -> IntentDecision:
        key = f"intent:v1:{self._model}:{query}"
        cached = self._cache.get(key)
        if cached is not None:
            if not isinstance(cached, IntentDecision):
                raise TypeError("intent cache value must be IntentDecision")
            return cached
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _INTENT_PROMPT},
                {"role": "user", "content": query},
            ],
            "temperature": 0.0,
            "max_tokens": 96,
            "response_format": {"type": "json_object"},
        }
        # Qwen3 thinking adds latency but no value for a five-way classifier.
        if self._model.startswith("Qwen/Qwen3-"):
            payload["enable_thinking"] = False
        try:
            response = self._http.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=bounded_http_timeout(self._timeout, timeout_seconds),
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            decision = self._parse(content)
        except ExternalServiceError:
            raise
        except Exception as exc:
            raise external_http_error(
                "siliconflow", "intent_classification", exc
            ) from exc
        self._cache.set(key, decision, self._cache_ttl)
        return decision

    @staticmethod
    def _parse(content: object) -> IntentDecision:
        if not isinstance(content, str):
            raise TypeError("intent response content must be str")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("intent response must be a JSON object")
        source_types = value.get("source_types")
        if not isinstance(source_types, list) or any(
            not isinstance(source, str) for source in source_types
        ):
            raise ValueError("source_types must be a string array")
        confidence = value.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be a number")
        legal_mode = value.get("legal_mode")
        if legal_mode is not None and not isinstance(legal_mode, str):
            raise ValueError("legal_mode must be a string or null")
        intent = value.get("intent")
        if not isinstance(intent, str):
            raise ValueError("intent must be a string")
        return IntentDecision(
            intent=intent,  # type: ignore[arg-type]
            source_types=tuple(source_types),  # type: ignore[arg-type]
            confidence=float(confidence),
            legal_mode=legal_mode,  # type: ignore[arg-type]
        )
