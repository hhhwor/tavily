"""进程内 MCP server:把搜索引擎包成 MCP 工具,与 FastAPI 同进程挂载。

传输:Streamable HTTP(stateless + JSON 响应),由 src/api.py 挂在主应用 `/mcp` 下。
工具:
- search —— query → 结构化 evidence[] 结果,正文截断、LLM-ready。
- verify_claims —— 候选陈述 + search evidence → 支持/冲突/证据不足。
- get_pdf_text —— 用 search 返回的 work_id + next_cursor 续读 PDF 正文。

搜索用例是同步阻塞(共享 Executor + 网络 + 重排),故在异步工具里用
anyio.to_thread 卸到线程池,避免阻塞事件循环影响并发请求。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.config import Settings
from src.engine import SearchEngine
from src.interfaces.presenters import McpSearchPresenter
from src.interfaces.schemas import search_command_from_mapping
from src.domain.evidence import Evidence, SearchBoundary
from src.domain.trust import CandidateClaim


def _transport_security(settings: Settings) -> Optional[TransportSecuritySettings]:
    """由不可变配置生成 DNS rebinding 防护(环境只在 bootstrap 读取)。

    - MCP_DNS_REBINDING_PROTECTION=false → 关闭 Host/Origin 校验(可信反代 + token 鉴权场景)。
    - 否则按 MCP_ALLOWED_HOSTS / MCP_ALLOWED_ORIGINS(逗号分隔,精确匹配或 `host:*` 端口通配)放行。
    - 都不设 → 返回 None(SDK 默认仅放行 localhost)。
    """
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
    """构建挂载用的 FastMCP 实例(复用传入的引擎单例)。"""
    settings = settings or Settings()
    mcp = FastMCP(
        "chukonu-web-search",
        instructions=(
            "面向 LLM 的多源 Web 检索:聚合腾讯/百度/SerpAPI,可选 OpenAlex 学术 + "
            "houdutech 专利 ES;跨源去重 + cross-encoder 重排,返回 LLM-ready 结构化结果。"
        ),
        stateless_http=True,      # 每次请求独立,无会话状态
        json_response=True,       # 直接返回 JSON(非 SSE 流),便于无状态调用
        streamable_http_path="/mcp",  # 子应用挂在根("/")时,端点即规范的 /mcp(无重定向)
        transport_security=_transport_security(settings),  # 反代/隧道后放行公网 Host(见上)
    )

    @mcp.tool(
        name="search",
        description=(
            "搜索网络并返回 LLM-ready 的结构化结果。当回答需要外部或最新信息"
            "(新闻、事实核查、技术细节、时效性问题)时调用,而不要凭记忆作答。"
            "学术与专利意图会自动识别;也可用 include_academic / include_patent 强制开关。"
            "返回按相关性混排的 evidence[],每条证据包含类型、来源、引用元数据、正文片段、"
            "授权/PDF 状态和诊断信息。trust_mode=annotate 时还返回出处、原文定位、证据质量"
            "和本次检索边界;这些字段不等同于陈述已被验证。先检查 answerability.gaps 与 failures; "
            "partial_failure=true 表示至少一路子任务失败但已有证据仍可使用。"
        ),
    )
    async def search(
        query: str,
        top_k: int = 0,
        include_academic: Optional[bool] = None,
        include_patent: Optional[bool] = None,
        ranking_profile: Optional[Literal["fast", "semantic", "quality"]] = None,
        rerank_threshold: Optional[float] = None,
        rerank_threshold_mode: Optional[Literal["off", "prefer", "strict"]] = None,
        rerank: Optional[bool] = None,
        fusion_enabled: Optional[bool] = None,
        include_pdf_text: bool = False,
        pdf_text_mode: Optional[Literal["cached", "sync"]] = None,
        pdf_max_results: Optional[int] = None,
        pdf_max_chars_per_result: Optional[int] = None,
        pdf_timeout_ms: Optional[int] = None,
        rewrite_enabled: Optional[bool] = None,
        trust_mode: Literal["off", "annotate"] = "annotate",
    ) -> dict[str, Any]:
        """query: 检索词。top_k: 返回条数(0 使用服务端默认)。
        include_academic / include_patent: None=按查询意图自动判定,true=强制开,false=强制关。
        ranking_profile: fast=快速先验排序,semantic=纯语义重排,quality=语义+领域信号。
        rerank_threshold: 0-1 的语义相关性阈值。0 等同关闭阈值。
        rerank_threshold_mode: off=不使用,prefer=达标项优先并回填,strict=删除未达标项。
        rerank: 已废弃的兼容别名;None=服务端默认,true=启用,false=fast。
        fusion_enabled: 已废弃的兼容别名;true=quality,false=semantic。
        include_pdf_text: true 时对重排后的前几篇学术结果同步补 PDF 正文。
        pdf_text_mode: cached 只读缓存,sync 允许本次请求下载解析。
        trust_mode: annotate 补可信 Phase 0 标注;off 返回旧 evidence 结构。"""
        command = search_command_from_mapping(
            locals(), aliases={"rerank": "rerank_enabled"}
        )
        resp = await anyio.to_thread.run_sync(
            lambda: engine.execute(command)
        )
        return McpSearchPresenter.present(resp)

    @mcp.tool(
        name="verify_claims",
        description=(
            "校验候选事实陈述是否被 search evidence 的可定位原文支持。返回每条陈述的"
            "supported/conflicted/insufficient 状态、支持/冲突引用、一致性检查、缺口和后续"
            "检索词。Phase 1 不自动补充检索;摘要、snippet 或无稳定 locator 的证据不能形成"
            "合格支持。"
        ),
    )
    async def verify_claims(
        query: str,
        claims: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        profile: str = "general",
        search_boundary: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """claims: CandidateClaim 对象列表。evidence: search 返回的 evidence[]。"""
        claim_models = [CandidateClaim.model_validate(item) for item in claims]
        evidence_models = [Evidence.model_validate(item) for item in evidence]
        boundary_model = (
            SearchBoundary.model_validate(search_boundary) if search_boundary else None
        )
        response = await anyio.to_thread.run_sync(
            lambda: engine.verify_claims(
                query,
                claim_models,
                evidence_models,
                profile=profile,
                search_boundary=boundary_model,
            )
        )
        return response.model_dump(mode="json")

    @mcp.tool(
        name="get_pdf_text",
        description=(
            "分页读取已抽取的 OpenAlex PDF 正文。先调用 search(include_pdf_text=true),"
            "从 academic evidence 的 citation.work_id 和 access.next_cursor 取参数;"
            "返回 text、page_from/page_to、next_cursor。此工具只读缓存,不触发下载解析。"
        ),
    )
    async def get_pdf_text(
        work_id: str,
        cursor: Optional[str] = None,
        max_chars: int = 8000,
    ) -> dict[str, Any]:
        """work_id: OpenAlex work id。cursor: search/access.next_cursor 或上次返回的 next_cursor。"""
        resp = await anyio.to_thread.run_sync(
            lambda: engine.get_pdf_text(work_id, cursor=cursor, max_chars=max_chars)
        )
        return resp.model_dump()

    return mcp
