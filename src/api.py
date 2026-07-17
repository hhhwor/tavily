"""FastAPI 传输层；运行资源在 lifespan 中由 ``src.bootstrap`` 装配。"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from src.bootstrap import Container, build_container
from src.interfaces.schemas import SearchRequest, VerifyRequest
from src.models import PdfTextResponse, SearchResponse, VerifyResponse

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


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
            return runtime().engine.execute(req.to_command())
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
