"""FastAPI 服务:面向 Agent 的搜索 API + 网页端 + 进程内 MCP server。

启动:  cd /home/ec2-user/tavily && .venv311/bin/uvicorn src.api:app --host 0.0.0.0 --port 8000
网页:  浏览器打开 http://<host>:8000/
调用:  POST /search  {"query": "...", "top_k": 10}
MCP:   Streamable HTTP 端点 http://<host>:8000/mcp(工具 search;同进程挂载)
"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src.config import settings
from src.engine import SearchEngine
from src.models import SearchResponse

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

engine = SearchEngine()

# 进程内 MCP:复用同一引擎单例;streamable_http_app() 惰性创建 session_manager。
# mcp 需 Python ≥3.10 —— 在旧 3.9 .venv 下导入失败时降级为「仅 REST」,不影响 /search。
try:
    from src.mcp_server import build_mcp

    _mcp = build_mcp(engine)
    _mcp_app = _mcp.streamable_http_app()
except Exception as e:  # mcp 未安装(如 3.9 venv)或构建失败
    print(f"[api] MCP 未挂载(降级为仅 REST): {e}")
    _mcp = None
    _mcp_app = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # MCP Streamable HTTP 需要在应用生命周期内运行 session manager(子应用挂载不会自动启动)
    if _mcp is not None:
        async with _mcp.session_manager.run():
            yield
    else:
        yield


app = FastAPI(title="Agent Search Engine (MVP)", version="0.1.0", lifespan=lifespan)

# 鉴权:配了 API_AUTH_TOKEN 时,对受保护路径强制 Bearer / X-API-Key 校验。
# 公开放行(便于网页加载、健康检查、读文档;真正的数据出口 /search 与 /mcp 受保护):
_PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _request_token(request: Request) -> str:
    """从 Authorization: Bearer 或 X-API-Key 取 token。"""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # 同时覆盖 REST(/search)与挂载的 MCP 子应用(/mcp);公开路径放行
    if settings.auth_enabled and request.url.path not in _PUBLIC_PATHS:
        token = _request_token(request)
        ok = bool(token) and any(hmac.compare_digest(token, t) for t in settings.auth_tokens)
        if not ok:
            return JSONResponse(
                {"detail": "缺少或无效的 API token(Authorization: Bearer <token> 或 X-API-Key)"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)



@app.get("/")
def index() -> FileResponse:
    """网页搜索界面。"""
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="搜索查询")
    top_k: int = Field(0, ge=0, le=50, description="返回条数,0 用默认")
    include_academic: Optional[bool] = Field(
        None,
        description="学术论文检索(OpenAlex):None=按查询意图自动判定,true=强制开,false=强制关",
    )
    include_patent: Optional[bool] = Field(
        None,
        description="专利检索(ES):None=按查询意图自动判定,true=强制开,false=强制关",
    )
    include_pdf_text: bool = Field(
        False,
        description="是否对学术结果返回 PDF 抽取正文。默认关闭；只处理重排后的前几条。",
    )
    pdf_text_mode: Optional[str] = Field(None, description="PDF 正文模式: cached / sync")
    pdf_max_results: Optional[int] = Field(None, ge=0, le=5, description="最多富化几条学术结果")
    pdf_max_chars_per_result: Optional[int] = Field(
        None, ge=1, le=30000, description="每条 PDF 正文最多返回字符数"
    )
    pdf_timeout_ms: Optional[int] = Field(None, ge=1000, le=60000, description="单篇 PDF 同步抽取预算")
    # 以下参数留空(None)则用服务端全局默认;网页端「高级选项」可按请求覆盖
    rerank_enabled: Optional[bool] = Field(None, description="是否启用 cross-encoder 重排")
    rerank_backend: Optional[str] = Field(
        None, description="重排后端:siliconflow / bge / flashrank / none"
    )
    rerank_model: Optional[str] = Field(None, description="重排模型,如 BAAI/bge-reranker-v2-m3")
    rerank_threshold: Optional[float] = Field(
        None, ge=0, le=1, description="相关性阈值,低于此分丢弃(0=不过滤)"
    )
    fusion_enabled: Optional[bool] = Field(None, description="是否启用辅助信号融合(新鲜度/权威度)")
    rewrite_enabled: Optional[bool] = Field(None, description="是否启用 L0 LLM 查询改写")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "providers": settings.enabled_providers,
        "academic": engine.academic_provider is not None,
        "patent": engine.patent_provider is not None,
        "reranker": engine.text_scorer.name,
        "auth": settings.auth_enabled,
        "mcp": _mcp_app is not None,
        "cache": engine.cache.stats() if engine.cache else {"enabled": False},
        "defaults": {
            "rerank_enabled": settings.rerank_enabled,
            "rerank_backend": settings.rerank_backend,
            "rerank_model": settings.rerank_model,
            "rerank_threshold": settings.rerank_threshold,
            "fusion_enabled": settings.fusion_enabled,
            "rewrite_enabled": settings.rewrite_enabled,
            "top_k": settings.default_top_k,
        },
    }


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    return engine.search(
        req.query,
        req.top_k,
        req.include_academic,
        req.include_patent,
        rerank_enabled=req.rerank_enabled,
        rerank_backend=req.rerank_backend,
        rerank_model=req.rerank_model,
        rerank_threshold=req.rerank_threshold,
        fusion_enabled=req.fusion_enabled,
        rewrite_enabled=req.rewrite_enabled,
        include_pdf_text=req.include_pdf_text,
        pdf_text_mode=req.pdf_text_mode,
        pdf_max_results=req.pdf_max_results,
        pdf_max_chars_per_result=req.pdf_max_chars_per_result,
        pdf_timeout_ms=req.pdf_timeout_ms,
    )


# 进程内 MCP server:Streamable HTTP 端点 /mcp(工具 search)。
# 挂在根("/")—— 显式路由(/、/health、/search、/docs)已先注册、优先匹配,
# 该 Mount 仅兜住 /mcp,从而给出无重定向的规范 /mcp 端点。未启用 MCP 时跳过。
if _mcp_app is not None:
    app.mount("/", _mcp_app)
