"""FreshQA response adapter for the production Doubao MCP client."""
from __future__ import annotations

import json
import os
from typing import Any

from src.infrastructure.doubao_mcp import (
    DOUBAO_MCP_REVISION,
    DoubaoMcpClient,
    fit_doubao_query,
)


def _passage(row: dict[str, Any], max_chars: int = 2000) -> str:
    parts: list[str] = []
    for name in ("Summary", "Snippet", "Content"):
        text = " ".join(str(row.get(name) or "").split())
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)[:max_chars]


def parse_doubao_response(
    text: str, *, query: str, query_adapted: bool, limit: int
) -> dict[str, Any]:
    """Validate and normalize one web_search response."""
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Doubao MCP returned non-JSON content") from exc
    result = body.get("Result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Doubao MCP returned no Result: {text[:200]}")
    rows = result.get("WebResults")
    if not isinstance(rows, list):
        raise RuntimeError(f"Doubao MCP returned no WebResults: {text[:200]}")
    evidence = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        evidence.append(
            {
                "source": "doubao",
                "type": "web",
                "title": str(row.get("Title") or ""),
                "url": str(row.get("Url") or ""),
                "passage": {"text": _passage(row)},
                "metadata": {
                    "site_name": row.get("SiteName"),
                    "publish_time": row.get("PublishTime"),
                    "auth_level": row.get("AuthInfoLevel"),
                    "rank_score": row.get("RankScore"),
                },
            }
        )
    gaps = []
    if query_adapted:
        gaps.append(
            {
                "code": "QUERY_LENGTH_ADAPTED",
                "severity": "info",
                "message": "Query was compacted to Doubao's 100-character limit.",
                "type": "web",
                "source": "doubao",
            }
        )
    return {
        "status": "complete",
        "evidence": evidence,
        "failures": [],
        "retrieval_assessment": {
            "status": "usable" if evidence else "limited",
            "quality_mix": {
                "citable": len(evidence),
                "limited": 0,
                "discovery_only": 0,
                "unavailable": 0,
            },
            "gaps": gaps,
            "query_sent": query,
        },
    }


class DoubaoMcpSearchClient:
    """Adapt the shared production client to the FreshQA evaluator contract."""

    def __init__(
        self,
        *,
        uvx_path: str,
        timeout: float,
        limit: int,
        api_key: str | None = None,
    ):
        if not 1 <= limit <= 50:
            raise ValueError("Doubao result limit must be between 1 and 50")
        key = api_key or os.environ.get("ASK_ECHO_SEARCH_INFINITY_API_KEY", "")
        if not key:
            raise ValueError("ASK_ECHO_SEARCH_INFINITY_API_KEY is required")
        self.timeout = timeout
        self.limit = limit
        self._client = DoubaoMcpClient(
            api_key=key,
            uvx_path=uvx_path,
        )

    def search(self, query: str) -> dict[str, Any]:
        fitted, adapted = fit_doubao_query(query)
        rows = self._client.search(
            fitted,
            count=self.limit,
            timeout=self.timeout,
        )
        text = json.dumps({"Result": {"WebResults": rows}}, ensure_ascii=False)
        return parse_doubao_response(
            text,
            query=fitted,
            query_adapted=adapted,
            limit=self.limit,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DoubaoMcpSearchClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
