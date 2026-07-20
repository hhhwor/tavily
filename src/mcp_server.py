"""MCP exposes exactly the same two capabilities as REST: search and research."""
from __future__ import annotations

from typing import Any, Literal, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.application.commands import ResearchFeedbackCommand
from src.config import Settings
from src.engine import SearchEngine
from src.interfaces.schemas import ResearchRequest, SearchRequest


def _transport_security(settings: Settings) -> Optional[TransportSecuritySettings]:
    if not settings.mcp_dns_rebinding_protection:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = list(settings.mcp_allowed_hosts)
    origins = list(settings.mcp_allowed_origins)
    if hosts or origins:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    return None


def build_mcp(engine: SearchEngine, settings: Optional[Settings] = None) -> FastMCP:
    settings = settings or Settings()
    mcp = FastMCP(
        "chukonu-search-research",
        instructions=(
            "先用 search 获取秒级 discovery evidence 与 search_id；当回答需要事实核验、"
            "反证检索、全文定位或覆盖评估时，用 research 启动并轮询研究任务。"
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=_transport_security(settings),
    )

    @mcp.tool(
        name="search",
        description=(
            "轻量多源搜索。返回 search.v1、discovery evidence、逐来源过滤执行情况、"
            "检索边界与可用于 research 的 search_id；结果相关不表示事实已被验证。"
        ),
    )
    async def search(
        query: str,
        limit: int = 10,
        source_types: Optional[list[Literal["web", "academic", "patent"]]] = None,
        filters: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        request = SearchRequest.model_validate({
            "query": query,
            "limit": limit,
            "source_types": source_types,
            "filters": filters or {},
        })
        response = await anyio.to_thread.run_sync(
            lambda: engine.execute(request.to_command())
        )
        return response.model_dump(mode="json")

    @mcp.tool(
        name="research",
        description=(
            "可信研究任务的统一生命周期工具。operation=start 需要 search_id；"
            "get 读取稳定任务 envelope；feedback 仅处理 needs_input；cancel 取消任务。"
            "dossier 分开报告任务停止原因和结论充分性，不返回伪精确 trust score。"
        ),
    )
    async def research(
        operation: Literal["start", "get", "feedback", "cancel"],
        search_id: Optional[str] = None,
        research_id: Optional[str] = None,
        profile: Literal[
            "literature_review",
            "technology_validation",
            "prior_art_landscape",
            "technology_landscape",
        ] = "technology_validation",
        depth: Literal["quick", "standard", "deep"] = "standard",
        objective: Optional[dict[str, Any]] = None,
        scope: Optional[dict[str, Any]] = None,
        policy: Optional[str] = None,
        budget: Optional[dict[str, Any]] = None,
        privacy: Optional[dict[str, Any]] = None,
        detail: Literal["standard", "full"] = "standard",
        task_revision: Optional[int] = None,
        answers: Optional[dict[str, str]] = None,
        note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        if operation == "start":
            if not search_id:
                raise ValueError("operation=start 必须提供 search_id")
            if not idempotency_key:
                raise ValueError("operation=start 必须提供 idempotency_key")
            request = ResearchRequest.model_validate({
                "search_id": search_id,
                "profile": profile,
                "depth": depth,
                "objective": objective,
                "scope": scope,
                "policy": policy,
                "budget": budget,
                "privacy": privacy,
            })
            response = await anyio.to_thread.run_sync(
                lambda: engine.start_research(
                    request.to_command(),
                    idempotency_key=idempotency_key,
                )
            )
        elif operation == "get":
            if not research_id:
                raise ValueError("operation=get 必须提供 research_id")
            response = await anyio.to_thread.run_sync(
                lambda: engine.get_research(research_id, detail=detail)
            )
        elif operation == "feedback":
            if not research_id or task_revision is None:
                raise ValueError("operation=feedback 必须提供 research_id 和 task_revision")
            command = ResearchFeedbackCommand(
                task_revision=task_revision,
                answers=answers or {},
                note=note,
            )
            response = await anyio.to_thread.run_sync(
                lambda: engine.research_feedback(research_id, command)
            )
        else:
            if not research_id:
                raise ValueError("operation=cancel 必须提供 research_id")
            response = await anyio.to_thread.run_sync(
                lambda: engine.cancel_research(
                    research_id,
                    task_revision=task_revision,
                )
            )
        return response.model_dump(mode="json")

    return mcp
