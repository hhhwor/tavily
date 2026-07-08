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
    work_id: str = ""                                  # OpenAlex work id
    year: Optional[int] = None                         # 出版年份
    venue: str = ""                                    # 期刊/会议名
    citations: int = 0                                 # 被引次数
    doi: str = ""                                       # DOI
    oa_url: str = ""                                    # 兼容字段:泛化 OA 链接(优先 landing,退化 pdf)
    oa_landing_url: str = ""                            # 开放获取落地页(HTML/DOI)
    oa_pdf_url: str = ""                                # 开放获取 PDF 直链
    license: str = ""                                   # OA location license
    license_id: str = ""                                # OpenAlex license id
    is_oa: bool = False                                 # 是否开放获取
    oa_status: str = ""                                 # 开放获取状态(gold/green/hybrid/bronze/diamond/closed)
    topic: str = ""                                     # 主要主题/学科
    pdf_status: str = "not_requested"                   # PDF 正文抽取状态
    pdf_text: str = ""                                  # PDF 抽取正文片段，不覆盖摘要 content
    pdf_pages: Optional[int] = None
    pdf_text_length: int = 0
    pdf_returned_chars: int = 0
    pdf_next_cursor: Optional[str] = None
    pdf_error_code: Optional[str] = None
    pdf_error_message: Optional[str] = None


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


class EvidencePassage(BaseModel):
    """Agent 可直接使用的证据文本片段。"""

    text: str
    snippet_type: str = ""                             # abstract/pdf_text/web_content/patent_abstract/...
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    chunk_index: Optional[int] = None


class EvidenceCitation(BaseModel):
    """用于生成引用的跨来源字段。"""

    label: str = ""
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    doi: Optional[str] = None
    work_id: Optional[str] = None
    publication_number: Optional[str] = None


class EvidencePatent(BaseModel):
    """专利 evidence 的专属结构化字段。"""

    publication_number: str = ""
    application_number: str = ""
    applicant: List[str] = Field(default_factory=list)
    inventor: List[str] = Field(default_factory=list)
    ipc_main: str = ""
    cpc_main: str = ""
    country: str = ""
    status: str = ""
    family_id: str = ""
    application_date: str = ""
    publication_date: str = ""
    patent_type: str = ""
    citation_count: int = 0


class EvidenceScores(BaseModel):
    """证据排序和置信度信号。"""

    relevance: Optional[float] = None
    source_rank: Optional[int] = None
    rerank_score: Optional[float] = None
    freshness: Optional[float] = None
    authority: Optional[float] = None
    confidence: Optional[float] = None


class EvidenceAccess(BaseModel):
    """正文、授权和继续读取状态。"""

    is_open: bool = False
    license: Optional[str] = None
    oa_pdf_url: Optional[str] = None
    pdf_status: Optional[str] = None
    next_cursor: Optional[str] = None


class EvidenceDiagnostics(BaseModel):
    """证据完整性和失败状态。"""

    warnings: List[str] = Field(default_factory=list)
    partial: bool = False
    failure_code: Optional[str] = None


class Evidence(BaseModel):
    """跨 web / academic / patent 的统一证据单元。"""

    id: str
    result_id: str
    type: str                                         # web / academic / patent
    source: str = ""
    title: str = ""
    url: str = ""
    published_date: str = ""
    updated_date: Optional[str] = None
    language: Optional[str] = None
    passage: EvidencePassage
    citation: EvidenceCitation = Field(default_factory=EvidenceCitation)
    patent: Optional[EvidencePatent] = None
    scores: EvidenceScores = Field(default_factory=EvidenceScores)
    access: EvidenceAccess = Field(default_factory=EvidenceAccess)
    diagnostics: EvidenceDiagnostics = Field(default_factory=EvidenceDiagnostics)


class SearchFailure(BaseModel):
    """检索链路中的可恢复失败,用于显式表达 partial failure。"""

    stage: str                                      # provider_search / rerank / pdf_enrichment / routing
    source: str = ""                                # provider 名或 evidence/result id
    type: Optional[str] = None                      # web / academic / patent
    code: str = ""
    message: str = ""
    recoverable: bool = True


class AnswerabilityGap(BaseModel):
    """Agent 需要知道的证据缺口。"""

    code: str
    severity: str = "warning"                       # info / warning / blocking
    message: str
    type: Optional[str] = None                      # web / academic / patent
    source: Optional[str] = None


class Answerability(BaseModel):
    """当前 evidence 是否足以支撑回答。"""

    status: str = "not_answerable"                  # answerable / partial / not_answerable
    confidence: str = "none"                        # high / medium / low / none
    gaps: List[AnswerabilityGap] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """/search 接口返回体。"""

    query: str
    normalized_query: str = ""
    rewritten_query: Optional[str] = None   # LLM 改写后的查询(若有)
    recency: Optional[str] = None
    time_sensitive: bool = False
    evidence: List[Evidence] = Field(default_factory=list)  # 给 Agent 直接使用的跨来源证据
    partial_failure: bool = False
    failures: List[SearchFailure] = Field(default_factory=list)
    answerability: Answerability = Field(default_factory=Answerability)
    count: int
    providers_used: List[str]
    reranker: str
    elapsed_ms: int


class PdfTextResponse(BaseModel):
    """分页读取 PDF 正文的返回体。"""

    work_id: str
    status: str
    chunk_index: Optional[int] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    text: Optional[str] = None
    returned_chars: int = 0
    next_cursor: Optional[str] = None
    partial: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None


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
    failures: List[SearchFailure] = Field(default_factory=list)
