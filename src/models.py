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


class AcademicResult(SearchResult):
    """学术论文结果(继承 SearchResult,故可直接喂给现有 reranker)。

    额外承载学术元数据。`content` 复用为论文摘要,`text_for_rerank()` 因此
    自动用「标题+摘要」打分,无需重写。
    """

    authors: List[str] = Field(default_factory=list)  # 作者名列表
    year: Optional[int] = None                         # 出版年份
    venue: str = ""                                    # 期刊/会议名
    citations: int = 0                                 # 被引次数
    doi: str = ""                                       # DOI
    oa_url: str = ""                                    # 兼容字段:泛化 OA 链接(优先 landing,退化 pdf)
    oa_landing_url: str = ""                            # 开放获取落地页(HTML/DOI)
    oa_pdf_url: str = ""                                # 开放获取 PDF 直链
    is_oa: bool = False                                 # 是否开放获取
    oa_status: str = ""                                 # 开放获取状态(gold/green/hybrid/bronze/diamond/closed)
    topic: str = ""                                     # 主要主题/学科


class PatentResult(SearchResult):
    """专利检索结果(继承 SearchResult,故可直接喂给现有 reranker)。

    `content` 复用为专利摘要,`text_for_rerank()` 因此自动用「专利名+摘要」
    打分,无需重写。额外承载专利元数据(对齐 EPO DOCDB / epo_docdb_v2 schema)。
    """

    publication_number: str = ""                       # 公开号(含国别前缀,如 US-2024030484-A1)
    application_number: str = ""                       # 申请号
    applicant: List[str] = Field(default_factory=list)  # 申请人
    inventor: List[str] = Field(default_factory=list)   # 发明人
    ipc_main: str = ""                                 # IPC 主分类号(本库较稀疏)
    cpc_main: str = ""                                 # CPC 主分类号
    application_date: str = ""                         # 申请日
    publication_date: str = ""                         # 公开日
    patent_type: str = ""                              # 专利类型/文献种类码(如 A1/A/B)
    country: str = ""                                  # 国别(US/CN/...)
    status: str = ""                                   # 法律状态
    family_id: str = ""                                # 同族 ID
    citation_count: int = 0                            # 引用计数


class SearchResponse(BaseModel):
    """/search 接口返回体。"""

    query: str
    normalized_query: str = ""
    rewritten_query: Optional[str] = None   # LLM 改写后的查询(若有)
    recency: Optional[str] = None
    time_sensitive: bool = False
    results: List[SearchResult]
    academic_results: List[AcademicResult] = Field(default_factory=list)  # 学术论文(独立成块)
    academic_query: Optional[str] = None    # 学术检索实际用的(改写后)查询,便于调试/评测
    patent_results: List[PatentResult] = Field(default_factory=list)  # 专利(独立成块)
    patent_query: Optional[str] = None      # 专利检索实际用的查询,便于调试/评测
    count: int
    providers_used: List[str]
    reranker: str
    elapsed_ms: int


class SearchPlan(BaseModel):
    """L0 查询理解层的产出 —— 引擎据此执行检索。"""

    raw_query: str
    normalized_query: str
    rewritten_query: Optional[str] = None   # LLM 改写后的查询(若有)
    recency: Optional[str] = None      # day / week / month / year(None=不限时效)
    time_sensitive: bool = False       # 驱动缓存 TTL 分级
    academic: bool = False             # 是否触发学术检索(OpenAlex)
    patent: bool = False               # 是否触发专利检索(ES)
    providers: List[str] = Field(default_factory=list)  # 路由:用哪些来源
    top_k: int = 10
