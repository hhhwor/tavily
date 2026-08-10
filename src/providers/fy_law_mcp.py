"""FY 法律法规 MCP 的 streamable HTTP 检索适配器。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

import requests

from src.application.ports.retrieval import RetrievalRequest, SourceDescriptor
from src.domain.errors import ExternalServiceError
from src.domain.search import SearchResult
from src.infrastructure.http_errors import external_http_error
from src.providers.base import SearchProvider


_DEFAULT_ENDPOINT = "https://api.cjbdi.com:8443/354347/mcp_law_service"
_DEFAULT_STATUS = "现行有效"
_ARTICLE = re.compile(
    r"第(?:[0-9０-９]+|[一二三四五六七八九十百千万零〇]+)条(?:之[一二三四五六七八九十]+)?"
)
_BRACKETED_TITLE = re.compile(r"《\s*([^《》]{2,80}?)\s*》")
_LAW_ALIASES = {
    "民法典": "中华人民共和国民法典",
    "刑法": "中华人民共和国刑法",
    "刑事诉讼法": "中华人民共和国刑事诉讼法",
    "民事诉讼法": "中华人民共和国民事诉讼法",
    "行政诉讼法": "中华人民共和国行政诉讼法",
    "劳动合同法": "中华人民共和国劳动合同法",
    "公司法": "中华人民共和国公司法",
}


class FyLawMcpProvider(SearchProvider):
    """通过 FY MCP 的法规检索工具返回标准 ``SearchResult``。

    供应商目前未返回 ``Mcp-Session-Id``，因此每次检索都按 streamable
    HTTP 标准完成 ``initialize -> notifications/initialized -> tools/call``。
    若服务端后续开始返回 session id，客户端会将它带入后续两个请求。
    """

    name = "fy_law_mcp"
    descriptor = SourceDescriptor(
        id=name,
        kind="legal",
        capabilities=frozenset({"full_content", "snippet"}),
        default_snapshot="mcp-server:法律法规检索服务",
        data_license="FY-provider-terms",
        default_language="zh",
        jurisdictions=("CN",),
        count_empty_as_used=True,
    )

    def __init__(
        self,
        *,
        endpoint: str = _DEFAULT_ENDPOINT,
        token: str,
        token_file: str = "",
        timeout: int = 15,
        http_session: Optional[requests.Session] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token.strip()
        self.token_file = Path(token_file) if token_file.strip() else None
        self.timeout = timeout
        self._http = http_session or requests
        self._status_lock = RLock()
        self._last_success_at: str | None = None
        self._last_failure_code: str | None = None
        self._last_auth_failure_at: str | None = None
        self._last_result_count: int | None = None
        if not self.endpoint:
            raise ValueError("缺少 FY 法规 MCP 地址: FY_LAW_MCP_URL")
        if not self.token and self.token_file is None:
            raise ValueError(
                "缺少 FY 法规 MCP Token: FY_LAW_MCP_TOKEN 或 "
                "FY_LAW_MCP_TOKEN_FILE"
            )

    def actual_filters(self, request: RetrievalRequest) -> Mapping[str, Any]:
        return {
            "tool": "flfg_iterative_search_tool",
            "status": request.legal_status or _DEFAULT_STATUS,
        }

    def applied_request_filters(
        self,
        request: RetrievalRequest,
    ) -> Mapping[str, Any]:
        return (
            {"legal_status": request.legal_status}
            if request.legal_status
            else {}
        )

    def limitations(self, request: RetrievalRequest) -> tuple[str, ...]:
        limitations = list(super().limitations(request))
        if any(item.upper() != "CN" for item in request.jurisdictions):
            limitations.append("JURISDICTION_FILTER_UNSUPPORTED")
        return tuple(dict.fromkeys(limitations))

    def search(
        self,
        query: str,
        top_k: int = 10,
        recency: Optional[str] = None,
    ) -> list[SearchResult]:
        return self.search_request(RetrievalRequest(
            query=query,
            candidate_budget=top_k,
            recency=recency,
        ))

    def search_request(self, request: RetrievalRequest) -> list[SearchResult]:
        arguments = {
            "query": self._query_arguments(request.query),
            "status": request.legal_status or _DEFAULT_STATUS,
        }
        try:
            response = self._call_tool(
                "flfg_iterative_search_tool",
                arguments,
                request,
            )
            records = self._records(response)
        except ExternalServiceError as exc:
            self._record_failure(exc.code)
            raise
        except Exception as exc:
            error = external_http_error(self.name, "mcp_law_search", exc)
            self._record_failure(error.code)
            raise error from exc
        results = self._normalize(records)[:request.candidate_budget]
        self._record_success(len(results))
        return results

    def runtime_status(self) -> dict[str, Any]:
        """返回可进入 health 的匿名运行状态，不泄露 endpoint 或 token。"""
        with self._status_lock:
            return {
                "enabled": True,
                "token_source": "file" if self.token_file is not None else "environment",
                "last_success_at": self._last_success_at,
                "last_failure_code": self._last_failure_code,
                "last_auth_failure_at": self._last_auth_failure_at,
                "last_result_count": self._last_result_count,
            }

    def _record_success(self, result_count: int) -> None:
        with self._status_lock:
            self._last_success_at = datetime.now(timezone.utc).isoformat()
            self._last_failure_code = None
            self._last_result_count = result_count

    def _record_failure(self, code: str) -> None:
        with self._status_lock:
            self._last_failure_code = code
            if code.endswith("AUTH_FAILED") or code == "MCP_TOKEN_UNAVAILABLE":
                self._last_auth_failure_at = datetime.now(timezone.utc).isoformat()

    def _token_snapshot(self) -> str:
        if self.token_file is None:
            return self.token
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ExternalServiceError(
                provider=self.name,
                code="MCP_TOKEN_UNAVAILABLE",
                recoverable=False,
                cause=exc,
            ) from exc
        if not token:
            raise ExternalServiceError(
                provider=self.name,
                code="MCP_TOKEN_UNAVAILABLE",
                recoverable=False,
                cause=ValueError("FY MCP token file is empty"),
            )
        return token

    def _call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        request: RetrievalRequest,
    ) -> Mapping[str, Any]:
        token = self._token_snapshot()
        session_id = self._initialize(request, token)
        self._notify_initialized(request, token, session_id)
        return self._rpc(
            method="tools/call",
            params={"name": tool_name, "arguments": dict(arguments)},
            request=request,
            token=token,
            session_id=session_id,
        )

    def _initialize(self, request: RetrievalRequest, token: str) -> str:
        response = self._rpc(
            method="initialize",
            params={
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "chukonu-web-search",
                    "version": "1.0",
                },
            },
            request=request,
            token=token,
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("MCP initialize 响应缺少 result")
        return str(response.get("_mcp_session_id") or "")

    def _notify_initialized(
        self,
        request: RetrievalRequest,
        token: str,
        session_id: str,
    ) -> None:
        self._rpc(
            method="notifications/initialized",
            params=None,
            request=request,
            token=token,
            session_id=session_id,
            notification=True,
        )

    def _rpc(
        self,
        *,
        method: str,
        params: Mapping[str, Any] | None,
        request: RetrievalRequest,
        token: str,
        session_id: str = "",
        notification: bool = False,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = f"fy-law-{uuid4().hex}"
        if params is not None:
            payload["params"] = dict(params)
        headers = {
            "COP-FYOP-AUTHORIZATION": token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        response = self._http.post(
            self.endpoint,
            json=payload,
            headers=headers,
            timeout=self.request_timeout(request),
        )
        response.raise_for_status()
        if notification:
            return {}
        decoded = response.json()
        if not isinstance(decoded, Mapping):
            raise ValueError("MCP 响应不是 JSON object")
        if decoded.get("error"):
            raise ExternalServiceError(
                provider=self.name,
                code="MCP_TOOL_REJECTED",
                recoverable=False,
                cause=RuntimeError("FY MCP returned a JSON-RPC error"),
            )
        session = getattr(response, "headers", {}).get("Mcp-Session-Id", "")
        return {**decoded, "_mcp_session_id": session}

    @staticmethod
    def _query_arguments(query: str) -> dict[str, str]:
        normalized = (query or "").strip()
        title = ""
        bracketed = _BRACKETED_TITLE.search(normalized)
        if bracketed:
            title = bracketed.group(1).strip()
        else:
            for alias, canonical in _LAW_ALIASES.items():
                if alias in normalized:
                    title = canonical
                    break
        item_match = _ARTICLE.search(normalized)
        item = item_match.group(0) if item_match else ""
        # FY 把 title、item、content 组合为收窄条件。法规名和条款都已明确时，
        # 继续把整句自然语言塞入 content 会把本应精确命中的法条过滤为空。
        content = "" if title and item else normalized
        return {
            "title": title,
            "item": item,
            "content": content,
        }

    @staticmethod
    def _records(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("MCP tools/call 响应缺少 result")
        if result.get("isError"):
            raise ExternalServiceError(
                provider=FyLawMcpProvider.name,
                code="MCP_TOOL_REJECTED",
                recoverable=False,
                cause=RuntimeError("FY MCP tool returned isError"),
            )
        records: list[Mapping[str, Any]] = []
        for block in result.get("content", ()):
            if not isinstance(block, Mapping) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, Mapping):
                values: Sequence[Any] = decoded.get("data", ()) if isinstance(
                    decoded.get("data"), list
                ) else (decoded,)
            elif isinstance(decoded, list):
                values = decoded
            else:
                values = ()
            records.extend(item for item in values if isinstance(item, Mapping))
        return records

    def _normalize(self, records: Sequence[Mapping[str, Any]]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for record in records:
            title = str(record.get("title") or "").strip()
            item = str(record.get("item") or "").strip()
            content = str(record.get("content") or "").strip()
            if not title or not content:
                continue
            display_title = " ".join(value for value in (title, item) if value)
            site = " · ".join(
                str(record.get(field) or "").strip()
                for field in ("law_type", "department", "status")
                if str(record.get(field) or "").strip()
            )
            score = record.get("score")
            results.append(SearchResult(
                url="",
                title=display_title,
                snippet=content[:400],
                content=content,
                site=site,
                score=float(score) if isinstance(score, (int, float)) else None,
                source=self.name,
                raw=dict(record),
            ))
        return results
