"""FastAPI transport for lightweight search and durable research tasks."""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from src.application.ports.research_store import (
    ResearchIdempotencyConflict,
    ResearchRevisionConflict,
    ResearchTaskNotFound,
)
from src.application.ports.search_seed import SearchSeedExpired, SearchSeedNotFound
from src.bootstrap import Container, build_container
from src.application.research_service import ResearchRequestError
from src.domain.research import ResearchTaskEnvelope
from src.domain.search_api import SearchResponse
from src.interfaces.schemas import (
    ResearchCancelRequest,
    ResearchDetail,
    ResearchFeedbackRequest,
    ResearchRequest,
    SearchRequest,
)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


@dataclass
class _RuntimeSlot:
    active: Optional[Container] = None


class _DeferredMcpApp:
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


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ResearchIdempotencyConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ResearchRequestError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (ResearchRevisionConflict, ValueError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (SearchSeedNotFound, ResearchTaskNotFound)):
        return HTTPException(status_code=404, detail="资源不存在")
    if isinstance(exc, SearchSeedExpired):
        return HTTPException(status_code=410, detail="search seed 已过期，请重新搜索")
    return HTTPException(status_code=500, detail="服务内部错误")


def create_app(
    container: Optional[Container] = None,
    *,
    container_factory: Callable[[], Container] = build_container,
) -> FastAPI:
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
        title="Agent Search and Research API",
        version="1.0.0",
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
                    {"detail": "缺少或无效的 API token"},
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
            "capabilities": ["search", "research"],
            "providers": list(settings.enabled_providers),
            "academic": engine.academic_provider is not None,
            "patent": engine.patent_provider is not None,
            "reranker": getattr(engine.text_scorer, "name", "unknown"),
            "research_verifier": getattr(classifier, "name", "unknown"),
            "auth": settings.auth_enabled,
            "mcp": current.mcp_available,
            "cache": engine.cache.stats() if engine.cache else {"enabled": False},
        }

    @application.post("/search", response_model=SearchResponse)
    def search(req: SearchRequest) -> SearchResponse:
        try:
            return runtime().engine.execute(req.to_command())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/research", response_model=ResearchTaskEnvelope, status_code=202)
    def start_research(
        req: ResearchRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ) -> ResearchTaskEnvelope:
        try:
            task = runtime().engine.start_research(
                req.to_command(),
                idempotency_key=idempotency_key,
            )
            response.headers["Location"] = task.links.self
            if task.retry_after_ms is not None:
                response.headers["Retry-After"] = str(
                    max(1, task.retry_after_ms // 1000)
                )
            return task
        except Exception as exc:
            raise _translate_error(exc) from exc

    @application.get("/research/{research_id}", response_model=ResearchTaskEnvelope)
    def get_research(
        research_id: str,
        response: Response,
        detail: ResearchDetail = Query("standard"),
        if_none_match: str | None = Header(None, alias="If-None-Match"),
    ) -> Any:
        try:
            task = runtime().engine.get_research(research_id, detail=detail)
            etag = f'W/"{task.task_revision}-{task.evidence_set_revision}"'
            if if_none_match == etag:
                return Response(status_code=304, headers={"ETag": etag})
            response.headers["ETag"] = etag
            if task.retry_after_ms is not None:
                response.headers["Retry-After"] = str(
                    max(1, task.retry_after_ms // 1000)
                )
            return task
        except Exception as exc:
            raise _translate_error(exc) from exc

    @application.post("/research/{research_id}/feedback", response_model=ResearchTaskEnvelope)
    def research_feedback(
        research_id: str,
        req: ResearchFeedbackRequest,
    ) -> ResearchTaskEnvelope:
        try:
            return runtime().engine.research_feedback(research_id, req.to_command())
        except Exception as exc:
            raise _translate_error(exc) from exc

    @application.post("/research/{research_id}/cancel", response_model=ResearchTaskEnvelope)
    def cancel_research(
        research_id: str,
        req: ResearchCancelRequest | None = None,
    ) -> ResearchTaskEnvelope:
        try:
            return runtime().engine.cancel_research(
                research_id,
                task_revision=req.task_revision if req else None,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    application.mount("/", _DeferredMcpApp(slot), name="mcp")
    return application


app = create_app()
