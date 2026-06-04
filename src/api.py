"""FastAPI 服务:面向 Agent 的搜索 API。

启动:  cd /data/tavily && .venv/bin/uvicorn src.api:app --host 0.0.0.0 --port 8000
调用:  POST /search  {"query": "...", "top_k": 10}
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from src.config import settings
from src.engine import SearchEngine
from src.models import SearchResponse

app = FastAPI(title="Agent Search Engine (MVP)", version="0.1.0")
engine = SearchEngine()


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="搜索查询")
    top_k: int = Field(0, ge=0, le=50, description="返回条数,0 用默认")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "providers": settings.enabled_providers,
        "reranker": engine.reranker.name,
    }


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    return engine.search(req.query, req.top_k)
