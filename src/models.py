"""归一化数据模型 —— 所有 Provider / 管线层共用。"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

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
    pdf_chunk_index: Optional[int] = None
    pdf_page_from: Optional[int] = None
    pdf_page_to: Optional[int] = None
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


class SearchBoundary(BaseModel):
    """本次检索实际运行的来源、时间、语言/辖区和预算边界。"""

    source_snapshot: Dict[str, str] = Field(default_factory=dict)
    query_time: str                              # UTC ISO-8601
    languages: List[str] = Field(default_factory=list)
    jurisdictions: List[str] = Field(default_factory=list)
    license_scope: List[str] = Field(default_factory=list)
    max_rounds: int = 1
    max_candidates: int = 0
    deadline_ms: Optional[int] = None
    limitations: List[str] = Field(default_factory=list)


class EvidenceFieldProvenance(BaseModel):
    """单个结构化字段来自哪里、经历过什么转换。"""

    source_field: Optional[str] = None
    retrieved_via: str = ""
    transformations: List[str] = Field(default_factory=list)


class EvidenceProvenance(BaseModel):
    """证据的发布者、版本、取得方式、许可与转换链。"""

    canonical_url: str = ""
    publisher_id: str = ""
    publisher_name: str = ""
    publisher_type: str = "unknown"
    retrieved_via: str = ""
    content_origin: str = "unknown"
    document_id: str = ""
    version_id: Optional[str] = None
    source_record_id: Optional[str] = None
    published_at: Optional[str] = None
    updated_at: Optional[str] = None
    retrieved_at: str                            # UTC ISO-8601
    ownership_group: Optional[str] = None
    syndication_group: Optional[str] = None
    license: Optional[str] = None
    original_language: Optional[str] = None
    parser_version: Optional[str] = None
    ocr_used: bool = False
    translation_used: bool = False
    field_provenance: Dict[str, EvidenceFieldProvenance] = Field(default_factory=dict)


class EvidenceLocator(BaseModel):
    """回到具体文档版本和原文单元所需的定位信息。"""

    document_id: str
    version_id: Optional[str] = None
    section: Optional[str] = None
    subsection: Optional[str] = None
    paragraph_id: Optional[str] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    table_id: Optional[str] = None
    figure_id: Optional[str] = None
    claim_number: Optional[str] = None
    chunk_index: Optional[int] = None


class EvidenceQuality(BaseModel):
    """证据当前能否用于陈述校验；不是来源真实性概率。"""

    level: str = "unavailable"  # citable / limited / discovery_only / unavailable
    is_original: bool = False
    has_stable_locator: bool = False
    can_support_key_claim: bool = False
    reasons: List[str] = Field(default_factory=list)


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
    provenance: Optional[EvidenceProvenance] = None
    locator: Optional[EvidenceLocator] = None
    quality: Optional[EvidenceQuality] = None


class CandidateClaim(BaseModel):
    """等待原文证据校验的最小事实性陈述。"""

    id: str
    text: str
    claim_type: str = "factual"
    importance: str = "key"                  # key / supporting / context
    subject: Optional[str] = None
    predicate: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    time_scope: Optional[str] = None
    jurisdiction: Optional[str] = None
    source: str = "agent"                    # user / agent / extractor
    parent_id: Optional[str] = None


class ConsistencyCheck(BaseModel):
    """陈述限定条件与证据原文的一项一致性检查。"""

    name: str                                 # entity/date/number/unit/negation/version/jurisdiction
    status: str = "unknown"                  # pass / fail / unknown
    claim_value: Optional[str] = None
    evidence_value: Optional[str] = None
    reason: str = ""


class ClaimEvidenceRelation(BaseModel):
    """一条 EvidenceUnit 对某条 CandidateClaim 的作用。"""

    evidence_id: str
    relation: str                             # supports / contradicts / mentions / unclear / irrelevant
    confidence: str = "none"                 # high / medium / low / none
    reason: str = ""
    quote: str = ""
    locator: Optional[EvidenceLocator] = None
    evidence_quality: str = "unavailable"
    qualified: bool = False                   # 是否可计入最终支持/冲突
    consistency_checks: List[ConsistencyCheck] = Field(default_factory=list)


class ClaimAssessment(BaseModel):
    """单条陈述的支持、冲突、证据不足及复核信息。"""

    claim: CandidateClaim
    status: str = "insufficient"             # supported/conflicted/insufficient/inference/needs_expert_review
    confidence: str = "none"
    relations: List[ClaimEvidenceRelation] = Field(default_factory=list)
    support_refs: List[str] = Field(default_factory=list)
    conflict_refs: List[str] = Field(default_factory=list)
    mention_refs: List[str] = Field(default_factory=list)
    independent_support_count: int = 0
    primary_source_count: int = 0
    counterevidence_searched: bool = False
    gaps: List[str] = Field(default_factory=list)
    followup_queries: List[str] = Field(default_factory=list)
    review_required: bool = False


class TrustAssessment(BaseModel):
    """本批候选陈述的校验汇总；不是真实性总分。"""

    status: str = "insufficient"             # supported / mixed / insufficient
    claims_total: int = 0
    supported_claims: int = 0
    conflicted_claims: int = 0
    insufficient_claims: int = 0
    evidence_coverage_rate: float = 0.0
    unsupported_statement_rate: float = 1.0
    policy_version: str = "trust-phase1-v1"
    model: str = "rules"
    warnings: List[str] = Field(default_factory=list)


class SearchFailure(BaseModel):
    """检索链路中的可恢复失败,用于显式表达 partial failure。"""

    stage: str                                      # provider_search / rerank / pdf_enrichment / routing
    source: str = ""                                # provider 名或 evidence/result id
    type: Optional[str] = None                      # web / academic / patent
    code: str = ""
    message: str = ""
    recoverable: bool = True


class VerifyResponse(BaseModel):
    """陈述级证据校验接口返回体。"""

    query: str
    profile: str = "general"
    assessments: List[ClaimAssessment] = Field(default_factory=list)
    trust_assessment: TrustAssessment = Field(default_factory=TrustAssessment)
    search_boundary: Optional[SearchBoundary] = None
    failures: List[SearchFailure] = Field(default_factory=list)
    elapsed_ms: int = 0


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
    trust_mode: str = "off"
    search_boundary: Optional[SearchBoundary] = None
    count: int
    providers_used: List[str]
    reranker: str
    # 带默认值以兼容 F01 之前写入的评测/响应缓存。
    ranking_profile: Literal["fast", "semantic", "quality"] = "quality"
    rerank_threshold: float = 0.3
    rerank_threshold_mode: Literal["off", "prefer", "strict"] = "prefer"
    ranking_warnings: List[str] = Field(default_factory=list)
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
