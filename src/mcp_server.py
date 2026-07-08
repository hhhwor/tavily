"""进程内 MCP server:把搜索引擎包成 MCP 工具,与 FastAPI 同进程挂载。

传输:Streamable HTTP(stateless + JSON 响应),由 src/api.py 挂在主应用 `/mcp` 下。
工具:
- search —— query → 结构化 evidence[] 结果,正文截断、LLM-ready。
- get_pdf_text —— 用 search 返回的 work_id + next_cursor 续读 PDF 正文。

引擎 search() 是同步阻塞(内部 ThreadPoolExecutor + 网络 + 重排),故在异步工具里用
anyio.to_thread 卸到线程池,避免阻塞事件循环影响并发请求。
"""
from __future__ import annotations

import os
from typing import Any, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.engine import SearchEngine


def _transport_security() -> Optional[TransportSecuritySettings]:
    """DNS rebinding 防护配置(放在反代/隧道后时需放行公网 Host)。

    - MCP_DNS_REBINDING_PROTECTION=false → 关闭 Host/Origin 校验(可信反代 + token 鉴权场景)。
    - 否则按 MCP_ALLOWED_HOSTS / MCP_ALLOWED_ORIGINS(逗号分隔,精确匹配或 `host:*` 端口通配)放行。
    - 都不设 → 返回 None(SDK 默认仅放行 localhost)。
    """
    if os.getenv("MCP_DNS_REBINDING_PROTECTION", "true").lower() == "false":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    origins = [o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if hosts or origins:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    return None


def _evidence_counts(evidence: list[Any]) -> dict[str, int]:
    counts = {"web": 0, "academic": 0, "patent": 0}
    for item in evidence:
        if item.type in counts:
            counts[item.type] += 1
    return counts


def build_mcp(engine: SearchEngine) -> FastMCP:
    """构建挂载用的 FastMCP 实例(复用传入的引擎单例)。"""
    mcp = FastMCP(
        "chukonu-web-search",
        instructions=(
            "面向 LLM 的多源 Web 检索:聚合腾讯/百度/SerpAPI,可选 OpenAlex 学术 + "
            "houdutech 专利 ES;跨源去重 + cross-encoder 重排,返回 LLM-ready 结构化结果。"
        ),
        stateless_http=True,      # 每次请求独立,无会话状态
        json_response=True,       # 直接返回 JSON(非 SSE 流),便于无状态调用
        streamable_http_path="/mcp",  # 子应用挂在根("/")时,端点即规范的 /mcp(无重定向)
        transport_security=_transport_security(),  # 反代/隧道后放行公网 Host(见上)
    )

    @mcp.tool(
        name="search",
        description=(
            "搜索网络并返回 LLM-ready 的结构化结果。当回答需要外部或最新信息"
            "(新闻、事实核查、技术细节、时效性问题)时调用,而不要凭记忆作答。"
            "学术与专利意图会自动识别;也可用 include_academic / include_patent 强制开关。"
            "返回按相关性混排的 evidence[],每条证据包含类型、来源、引用元数据、正文片段、"
            "授权/PDF 状态和诊断信息。先检查 answerability.gaps 与 failures; "
            "partial_failure=true 表示至少一路子任务失败但已有证据仍可使用。"
        ),
    )
    async def search(
        query: str,
        top_k: int = 10,
        include_academic: Optional[bool] = None,
        include_patent: Optional[bool] = None,
        rerank: Optional[bool] = None,
        include_pdf_text: bool = False,
        pdf_text_mode: Optional[str] = None,
        pdf_max_results: Optional[int] = None,
        pdf_max_chars_per_result: Optional[int] = None,
    ) -> dict[str, Any]:
        """query: 检索词。top_k: 返回条数(默认 10)。
        include_academic / include_patent: None=按查询意图自动判定,true=强制开,false=强制关。
        rerank: None=服务端默认,true=开 cross-encoder 重排(质量更高,慢数秒),false=走 RRF 快路径。
        include_pdf_text: true 时对重排后的前几篇学术结果同步补 PDF 正文。
        pdf_text_mode: cached 只读缓存,sync 允许本次请求下载解析。"""
        resp = await anyio.to_thread.run_sync(
            lambda: engine.search(
                query, top_k, include_academic, include_patent,
                rerank_enabled=rerank,
                include_pdf_text=include_pdf_text,
                pdf_text_mode=pdf_text_mode,
                pdf_max_results=pdf_max_results,
                pdf_max_chars_per_result=pdf_max_chars_per_result,
            )
        )

        return {
            "query": resp.query,
            "recency": resp.recency,
            "partial_failure": resp.partial_failure,
            "failures": [f.model_dump() for f in resp.failures],
            "answerability": resp.answerability.model_dump(),
            "evidence": [e.model_dump() for e in resp.evidence],
            "meta": {
                "providers_used": resp.providers_used,
                "reranker": resp.reranker,
                "elapsed_ms": resp.elapsed_ms,
                "counts": _evidence_counts(resp.evidence),
            },
        }

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
