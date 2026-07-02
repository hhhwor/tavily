"""进程内 MCP server:把搜索引擎包成 MCP 工具,与 FastAPI 同进程挂载。

传输:Streamable HTTP(stateless + JSON 响应),由 src/api.py 挂在主应用 `/mcp` 下。
工具:search —— query → 结构化 {web, academic, patents} 结果,正文截断、LLM-ready。

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

_CONTENT_CAP = 600  # 每条正文截断字符数,省 token


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


def _trim(s: Optional[str], n: int = _CONTENT_CAP) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + "…"


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
            "返回 web 结果,命中时附带学术论文(authors/year/venue/citations/doi/"
            "oa_landing_url/oa_pdf_url/is_oa/oa_status)与专利(公开号/申请人/发明人/分类)"
            "各成一块。"
        ),
    )
    async def search(
        query: str,
        top_k: int = 10,
        include_academic: Optional[bool] = None,
        include_patent: Optional[bool] = None,
        rerank: Optional[bool] = None,
    ) -> dict[str, Any]:
        """query: 检索词。top_k: 返回条数(默认 10)。
        include_academic / include_patent: None=按查询意图自动判定,true=强制开,false=强制关。
        rerank: None=服务端默认,true=开 cross-encoder 重排(质量更高,慢数秒),false=走 RRF 快路径。"""
        resp = await anyio.to_thread.run_sync(
            lambda: engine.search(
                query, top_k, include_academic, include_patent,
                rerank_enabled=rerank,
            )
        )

        def _web(r):
            return {
                "title": r.title, "url": r.url,
                "content": _trim(r.content or r.snippet),
                "score": r.rerank_score if r.rerank_score is not None else r.score,
                "source": r.source, "date": r.date,
            }

        def _paper(p):
            return {
                "title": p.title, "url": p.url,
                "oa_url": p.oa_url,
                "oa_landing_url": p.oa_landing_url,
                "oa_pdf_url": p.oa_pdf_url,
                "authors": p.authors[:6], "year": p.year, "venue": p.venue,
                "citations": p.citations, "doi": p.doi, "is_oa": p.is_oa,
                "oa_status": p.oa_status,
                "content": _trim(p.content or p.snippet),
            }

        def _pat(p):
            return {
                "title": p.title, "url": p.url,
                "publication_number": p.publication_number,
                "applicant": p.applicant, "inventor": p.inventor,
                "country": p.country, "classification": p.ipc_main or p.cpc_main,
                "application_date": p.application_date, "patent_type": p.patent_type,
                "content": _trim(p.content or p.snippet),
            }

        return {
            "query": resp.query,
            "recency": resp.recency,
            "web": [_web(r) for r in resp.results],
            "academic": [_paper(p) for p in resp.academic_results],
            "patents": [_pat(p) for p in resp.patent_results],
            "meta": {
                "providers_used": resp.providers_used,
                "reranker": resp.reranker,
                "elapsed_ms": resp.elapsed_ms,
                "counts": {
                    "web": len(resp.results),
                    "academic": len(resp.academic_results),
                    "patents": len(resp.patent_results),
                },
            },
        }

    return mcp
