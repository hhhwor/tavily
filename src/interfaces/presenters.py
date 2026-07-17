"""Explicit response projections for compact transports."""
from __future__ import annotations

from typing import Any

from src.interfaces.responses import SearchResponse


class McpSearchPresenter:
    schema_version = "mcp-search.v1"

    @staticmethod
    def _evidence_counts(response: SearchResponse) -> dict[str, int]:
        counts = {"web": 0, "academic": 0, "patent": 0}
        for item in response.evidence:
            if item.type in counts:
                counts[item.type] += 1
        return counts

    @classmethod
    def present(cls, response: SearchResponse) -> dict[str, Any]:
        return {
            "schema_version": cls.schema_version,
            "query": response.query,
            "normalized_query": response.normalized_query,
            "rewritten_query": response.rewritten_query,
            "recency": response.recency,
            "time_sensitive": response.time_sensitive,
            "partial_failure": response.partial_failure,
            "failures": [failure.model_dump(mode="json") for failure in response.failures],
            "answerability": response.answerability.model_dump(mode="json"),
            "trust_mode": response.trust_mode,
            "search_boundary": (
                response.search_boundary.model_dump(mode="json")
                if response.search_boundary
                else None
            ),
            "evidence": [item.model_dump(mode="json") for item in response.evidence],
            "count": response.count,
            "meta": {
                "providers_used": response.providers_used,
                "reranker": response.reranker,
                "ranking_profile": response.ranking_profile,
                "rerank_threshold": response.rerank_threshold,
                "rerank_threshold_mode": response.rerank_threshold_mode,
                "ranking_warnings": response.ranking_warnings,
                "elapsed_ms": response.elapsed_ms,
                "counts": cls._evidence_counts(response),
            },
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> SearchResponse:
        """Restore both v1 and the pre-versioned compact MCP projection."""
        version = data.get("schema_version")
        if version not in {None, cls.schema_version}:
            raise ValueError(f"unsupported MCP search schema: {version}")
        meta = data.get("meta") or {}
        evidence = data.get("evidence") or []
        return SearchResponse.model_validate({
            "query": data.get("query", ""),
            "normalized_query": data.get(
                "normalized_query", data.get("query", "")
            ),
            "rewritten_query": data.get("rewritten_query"),
            "recency": data.get("recency"),
            "time_sensitive": bool(data.get("time_sensitive")),
            "evidence": evidence,
            "partial_failure": bool(data.get("partial_failure")),
            "failures": data.get("failures") or [],
            "answerability": data.get("answerability") or {},
            "trust_mode": data.get("trust_mode", "off"),
            "search_boundary": data.get("search_boundary"),
            "count": int(data.get("count", len(evidence))),
            "providers_used": meta.get("providers_used") or [],
            "reranker": meta.get("reranker", ""),
            "ranking_profile": meta.get("ranking_profile", "quality"),
            "rerank_threshold": meta.get("rerank_threshold", 0.3),
            "rerank_threshold_mode": meta.get(
                "rerank_threshold_mode", "prefer"
            ),
            "ranking_warnings": meta.get("ranking_warnings") or [],
            "elapsed_ms": int(meta.get("elapsed_ms") or 0),
        })
