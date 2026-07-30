"""Doubao Search provider backed by the official Search Infinity MCP server."""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.domain.errors import ExternalServiceError
from src.domain.search import SearchResult
from src.infrastructure.doubao_mcp import (
    DOUBAO_MCP_REVISION,
    DoubaoMcpClient,
    fit_doubao_query,
)
from src.providers.base import SearchProvider


_RECENCY_TIME_RANGE = {
    "day": "OneDay",
    "week": "OneWeek",
    "month": "OneMonth",
    "year": "OneYear",
}


class _DoubaoClient(Protocol):
    def search(
        self,
        query: str,
        *,
        count: int,
        time_range: str | None = None,
        timeout: float,
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


def _score(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DoubaoSearchProvider(SearchProvider):
    """Expose Doubao as a normal concurrent web-recall source."""

    name = "doubao"
    descriptor = SourceDescriptor(
        id=name,
        kind="web",
        capabilities=frozenset({
            "recency_filter",
            "time_range_filter",
            "full_content",
            "snippet",
        }),
        default_snapshot=f"mcp-server:{DOUBAO_MCP_REVISION}",
        data_license="volcengine-search-infinity-terms",
        max_candidates=50,
        count_empty_as_used=True,
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 15,
        uvx_path: str = "",
        *,
        client: _DoubaoClient | None = None,
    ) -> None:
        self.api_key = api_key or ""
        self.timeout = timeout
        if not self.api_key:
            raise ValueError(
                "缺少豆包搜索凭证: ASK_ECHO_SEARCH_INFINITY_API_KEY"
            )
        self._client = client or DoubaoMcpClient(
            api_key=self.api_key,
            uvx_path=uvx_path,
        )

    @staticmethod
    def _time_range(
        *,
        recency: str | None,
        request: RetrievalRequest | None,
    ) -> str | None:
        if (
            request is not None
            and request.time_from is not None
            and request.time_to is not None
        ):
            start = request.time_from.date().isoformat()
            end = request.time_to.date().isoformat()
            return f"{start}..{end}"
        return _RECENCY_TIME_RANGE.get(recency or "")

    def actual_query(self, request: RetrievalRequest) -> str:
        return fit_doubao_query(request.query)[0]

    def actual_filters(self, request: RetrievalRequest) -> Mapping[str, Any]:
        filters: dict[str, Any] = {
            "Count": min(max(request.candidate_budget, 1), 50),
            "SearchType": "web",
        }
        time_range = self._time_range(
            recency=request.recency,
            request=request,
        )
        if time_range:
            filters["TimeRange"] = time_range
        return filters

    def applied_request_filters(
        self,
        request: RetrievalRequest,
    ) -> Mapping[str, Any]:
        if request.time_from is None or request.time_to is None:
            return {}
        return {
            "published_from": request.time_from.date().isoformat(),
            "published_to": request.time_to.date().isoformat(),
        }

    def search(
        self,
        query: str,
        top_k: int = 10,
        recency: Optional[str] = None,
    ) -> list[SearchResult]:
        return self._search(
            query,
            top_k,
            recency,
            request=None,
        )

    def search_request(
        self,
        request: RetrievalRequest,
    ) -> list[SearchResult]:
        return self._search(
            request.query,
            request.candidate_budget,
            request.recency,
            request=request,
        )

    def _search(
        self,
        query: str,
        top_k: int,
        recency: str | None,
        *,
        request: RetrievalRequest | None,
    ) -> list[SearchResult]:
        fitted_query, _ = fit_doubao_query(query)
        count = min(max(top_k, 1), 50)
        timeout = min(
            float(self.timeout),
            (
                float(request.timeout_seconds)
                if request is not None and request.timeout_seconds is not None
                else float(self.timeout)
            ),
        )
        try:
            rows = self._client.search(
                fitted_query,
                count=count,
                time_range=self._time_range(
                    recency=recency,
                    request=request,
                ),
                timeout=timeout,
            )
        except ExternalServiceError:
            raise
        except TimeoutError as exc:
            raise ExternalServiceError(
                provider=self.name,
                code="SEARCH_TIMEOUT",
                recoverable=True,
                cause=exc,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ExternalServiceError(
                provider=self.name,
                code="SEARCH_REQUEST_REJECTED",
                recoverable=False,
                cause=exc,
            ) from exc
        except Exception as exc:
            raise ExternalServiceError(
                provider=self.name,
                code="SEARCH_UPSTREAM_UNAVAILABLE",
                recoverable=True,
                cause=exc,
            ) from exc
        return self._normalize(rows)[:top_k]

    def _normalize(self, rows: list[dict[str, Any]]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for row in rows:
            url = str(row.get("Url") or "")
            if not url:
                continue
            content = str(
                row.get("Content")
                or row.get("Summary")
                or row.get("Snippet")
                or ""
            )
            snippet = str(
                row.get("Summary")
                or row.get("Snippet")
                or content[:500]
            )
            raw = {
                key: value
                for key, value in row.items()
                if key not in {"Content", "Summary", "Snippet"}
            }
            results.append(SearchResult(
                url=url,
                title=str(row.get("Title") or ""),
                snippet=snippet,
                content=content,
                date=str(row.get("PublishTime") or ""),
                site=str(row.get("SiteName") or ""),
                score=_score(row.get("RankScore")),
                source=self.name,
                raw=raw,
            ))
        return results

    def close(self) -> None:
        self._client.close()
