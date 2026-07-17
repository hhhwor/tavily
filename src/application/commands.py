"""Application 层公开命令契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class SearchCommand:
    """一次搜索请求的不可变输入。

    字段与 ``SearchEngine.search`` 的兼容入口一一对应；传输层只需完成
    schema 校验和 DTO 映射，不再把一组松散参数传遍应用服务。
    """

    query: str
    top_k: int = 0
    include_academic: Optional[bool] = None
    include_patent: Optional[bool] = None
    rerank_enabled: Optional[bool] = None
    rerank_backend: Optional[str] = None
    rerank_model: Optional[str] = None
    rerank_threshold: Optional[float] = None
    fusion_enabled: Optional[bool] = None
    ranking_profile: Optional[str] = None
    rerank_threshold_mode: Optional[str] = None
    rewrite_enabled: Optional[bool] = None
    trust_mode: str = "annotate"
    include_pdf_text: bool = False
    pdf_text_mode: Optional[str] = None
    pdf_max_results: Optional[int] = None
    pdf_max_chars_per_result: Optional[int] = None
    pdf_timeout_ms: Optional[int] = None
