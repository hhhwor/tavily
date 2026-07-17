"""Composition of text scorer adapters."""
from __future__ import annotations

from typing import Any, Optional

from src.ranking.adapters import BGEReranker, FlashRankReranker, SiliconFlowReranker
from src.ranking.ports import NoOpReranker, Reranker


def build_text_scorer(
    enabled: bool,
    backend: str,
    model_name: str,
    cache_dir: str,
    device: Optional[str] = None,
    chunk_max_chars: int = 400,
    chunk_overlap: int = 50,
    siliconflow_api_key: str = "",
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1",
    http_session: Any = None,
) -> Reranker:
    if not enabled or backend == "none":
        return NoOpReranker()
    try:
        if backend == "bge":
            return BGEReranker(
                model_name,
                device=device,
                chunk_max_chars=chunk_max_chars,
                chunk_overlap=chunk_overlap,
            )
        if backend == "flashrank":
            return FlashRankReranker(
                model_name,
                cache_dir,
                chunk_max_chars=chunk_max_chars,
                chunk_overlap=chunk_overlap,
            )
        if backend == "siliconflow":
            if not siliconflow_api_key:
                print("[rerank] siliconflow: 未配置 SILICONFLOW_API_KEY,回退 NoOp")
                return NoOpReranker()
            return SiliconFlowReranker(
                api_key=siliconflow_api_key,
                base_url=siliconflow_base_url,
                model=model_name,
                chunk_max_chars=chunk_max_chars,
                chunk_overlap=chunk_overlap,
                http_session=http_session,
            )
        return NoOpReranker()
    except Exception:
        print(f"[rerank] text_scorer backend={backend} 不可用,回退 NoOp")
        return NoOpReranker()
