"""归一化数据模型 —— 所有 Provider / 管线层共用。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    """单条搜索结果(各来源统一结构)。"""

    url: str
    title: str = ""
    snippet: str = ""                       # 摘要
    content: str = ""                       # 正文(若来源提供)
    date: str = ""                          # 发布时间
    site: str = ""                          # 来源站点
    score: Optional[float] = None           # 来源原始相关性分
    rerank_score: Optional[float] = None    # 重排/融合后分数
    provider_rank: Optional[int] = None     # 在所属来源结果中的原始排名(0-based)
    source: str = ""                        # 来源 Provider 名 (tencent/baidu/...)
    raw: Dict[str, Any] = Field(default_factory=dict, repr=False)

    def text_for_rerank(self) -> str:
        """供重排器打分的文本:优先正文,退化到摘要/标题。"""
        body = self.content or self.snippet or ""
        return f"{self.title}\n{body}".strip()


class SearchResponse(BaseModel):
    """/search 接口返回体。"""

    query: str
    normalized_query: str = ""
    recency: Optional[str] = None
    time_sensitive: bool = False
    results: List[SearchResult]
    count: int
    providers_used: List[str]
    reranker: str
    elapsed_ms: int


class SearchPlan(BaseModel):
    """L0 查询理解层的产出 —— 引擎据此执行检索。"""

    raw_query: str
    normalized_query: str
    recency: Optional[str] = None      # day / week / month / year(None=不限时效)
    time_sensitive: bool = False       # 驱动缓存 TTL 分级
    providers: List[str] = Field(default_factory=list)  # 路由:用哪些来源
    top_k: int = 10
