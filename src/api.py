"""FastAPI 传输层；运行资源在 lifespan 中由 ``src.bootstrap`` 装配。"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from src.bootstrap import Container, build_container
from src.models import (
    CandidateClaim,
    Evidence,
    PdfTextResponse,
    SearchBoundary,
    SearchResponse,
    VerifyResponse,
)
from src.pipeline.ranking_options import resolve_ranking_options

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


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
    ranking_profile: Optional[Literal["fast", "semantic", "quality"]] = Field(
        None,
        description=(
            "排序档位:fast=无文本模型;semantic=纯文本相关性;"
            "quality=文本相关性与领域辅助信号融合"
        ),
    )
    rerank_enabled: Optional[bool] = Field(
        None,
        description="兼容旧参数:false 映射 fast,true 启用服务端非 fast 档位",
        json_schema_extra={"deprecated": True},
    )
    rerank_backend: Optional[str] = Field(
        None, description="重排后端:siliconflow / bge / flashrank / none"
    )
    rerank_model: Optional[str] = Field(None, description="重排模型,如 BAAI/bge-reranker-v2-m3")
    rerank_threshold: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="文本相关性阈值；具体过滤/回填行为由 rerank_threshold_mode 决定",
    )
    rerank_threshold_mode: Optional[Literal["off", "prefer", "strict"]] = Field(
        None,
        description="阈值模式:off=关闭,prefer=达标优先且不足时回填,strict=删除未达标项",
    )
    fusion_enabled: Optional[bool] = Field(
        None,
        description="兼容旧参数:true 映射 quality,false 映射 semantic",
        json_schema_extra={"deprecated": True},
    )
    rewrite_enabled: Optional[bool] = Field(None, description="是否启用 L0 LLM 查询改写")
    trust_mode: Literal["off", "annotate"] = Field(
        "annotate",
        description="可信证据 Phase 0:off=保持旧 evidence;annotate=补 provenance/locator/quality/边界",
    )

    @model_validator(mode="after")
    def validate_explicit_ranking_options(self) -> "SearchRequest":
        """只校验请求内部冲突；服务端默认相关冲突由注入的 Engine 校验。"""
        resolve_ranking_options(
            default_profile="quality",
            default_threshold=0.3,
            default_threshold_mode="prefer",
            ranking_profile=self.ranking_profile,
            rerank_enabled=self.rerank_enabled,
            fusion_enabled=self.fusion_enabled,
            rerank_backend=self.rerank_backend,
            rerank_threshold=self.rerank_threshold,
            rerank_threshold_mode=self.rerank_threshold_mode,
        )
        return self


class VerifyRequest(BaseModel):
    query: str = Field(..., min_length=1, description="产生这些候选陈述的原始问题")
    claims: List[CandidateClaim] = Field(..., min_length=1, max_length=20)
    evidence: List[Evidence] = Field(..., max_length=100)
    profile: Literal[
        "general", "news", "scientific", "patent", "legal", "financial", "product"
    ] = "general"
    search_boundary: Optional[SearchBoundary] = None


@dataclass
class _RuntimeSlot:
    active: Optional[Container] = None


class _DeferredMcpApp:
    """固定 mount；请求时转发给 lifespan 中创建的 MCP ASGI app。"""

    def __init__(self, slot: _RuntimeSlot):
        self._slot = slot

    async def __call__(self, scope, receive, send) -> None:
        target = self._slot.active.mcp_app if self._slot.active else None
        if target is None:
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b"Not Found"})
            elif scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1000})
            return
        await target(scope, receive, send)


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def create_app(
    container: Optional[Container] = None,
    *,
    container_factory: Callable[[], Container] = build_container,
) -> FastAPI:
    """创建隔离的 API app；此调用本身不读取环境或创建运行资源。

    显式传入的 ``container`` 是一次性运行时；需要重复启动同一个 app（例如测试）
    时应传 ``container_factory``，使每次 lifespan 都获得新的 Container。
    """
    slot = _RuntimeSlot()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime = container if container is not None else container_factory()
        slot.active = runtime
        application.state.container = runtime
        try:
            async with runtime.lifespan():
                yield
        finally:
            slot.active = None
            application.state.container = None

    application = FastAPI(
        title="Agent Search Engine (MVP)",
        version="0.2.0",
        lifespan=lifespan,
    )

    def runtime() -> Container:
        if slot.active is None:
            raise HTTPException(status_code=503, detail="应用资源尚未启动")
        return slot.active

    @application.middleware("http")
    async def auth_middleware(request: Request, call_next):
        current = slot.active
        if current is None:
            return JSONResponse({"detail": "应用资源尚未启动"}, status_code=503)
        settings = current.settings
        if settings.auth_enabled and request.url.path not in _PUBLIC_PATHS:
            token = _request_token(request)
            ok = bool(token) and any(
                hmac.compare_digest(token, expected) for expected in settings.auth_tokens
            )
            if not ok:
                return JSONResponse(
                    {"detail": "缺少或无效的 API token(Authorization: Bearer <token> 或 X-API-Key)"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @application.get("/")
    def index() -> FileResponse:
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    @application.get("/health")
    def health() -> dict:
        current = runtime()
        settings = current.settings
        engine = current.engine
        classifier = getattr(engine.claim_verifier, "classifier", None)
        return {
            "status": "ok",
            "providers": list(settings.enabled_providers),
            "academic": engine.academic_provider is not None,
            "patent": engine.patent_provider is not None,
            "reranker": engine.text_scorer.name,
            "claim_verifier": getattr(classifier, "name", "unknown"),
            "auth": settings.auth_enabled,
            "mcp": current.mcp_available,
            "cache": engine.cache.stats() if engine.cache else {"enabled": False},
            "defaults": {
                "ranking_profile": settings.ranking_profile,
                "rerank_enabled": settings.rerank_enabled,
                "rerank_backend": settings.rerank_backend,
                "rerank_model": settings.rerank_model,
                "rerank_threshold": settings.rerank_threshold,
                "rerank_threshold_mode": settings.rerank_threshold_mode,
                "ranking_warnings": list(settings.ranking_warnings),
                "fusion_enabled": settings.fusion_enabled,
                "rewrite_enabled": settings.rewrite_enabled,
                "trust_mode": "annotate",
                "top_k": settings.default_top_k,
            },
        }

    @application.post("/search", response_model=SearchResponse)
    def search(req: SearchRequest) -> SearchResponse:
        try:
            return runtime().engine.search(
                req.query,
                req.top_k,
                req.include_academic,
                req.include_patent,
                ranking_profile=req.ranking_profile,
                rerank_enabled=req.rerank_enabled,
                rerank_backend=req.rerank_backend,
                rerank_model=req.rerank_model,
                rerank_threshold=req.rerank_threshold,
                rerank_threshold_mode=req.rerank_threshold_mode,
                fusion_enabled=req.fusion_enabled,
                rewrite_enabled=req.rewrite_enabled,
                trust_mode=req.trust_mode,
                include_pdf_text=req.include_pdf_text,
                pdf_text_mode=req.pdf_text_mode,
                pdf_max_results=req.pdf_max_results,
                pdf_max_chars_per_result=req.pdf_max_chars_per_result,
                pdf_timeout_ms=req.pdf_timeout_ms,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/verify", response_model=VerifyResponse)
    def verify(req: VerifyRequest) -> VerifyResponse:
        return runtime().engine.verify_claims(
            req.query,
            req.claims,
            req.evidence,
            profile=req.profile,
            search_boundary=req.search_boundary,
        )

    @application.get("/academic/pdf/text/{work_id}", response_model=PdfTextResponse)
    def get_pdf_text(
        work_id: str,
        cursor: Optional[str] = None,
        max_chars: int = 8000,
    ) -> PdfTextResponse:
        return runtime().engine.get_pdf_text(work_id, cursor=cursor, max_chars=max_chars)

    # 必须最后注册，避免 root mount 截获显式 REST 路由。
    application.mount("/", _DeferredMcpApp(slot), name="mcp")
    return application


# Uvicorn 兼容入口；这里只创建路由，配置和资源延迟到 lifespan。
app = create_app()
