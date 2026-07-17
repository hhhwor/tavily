"""配置:加载 .env + 集中管理 MVP 参数。"""
from __future__ import annotations

import os
from typing import List


def load_dotenv(path: str = "") -> None:
    """极简 .env 加载(不引入 python-dotenv)。"""
    path = path or os.path.join(_project_root(), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class Settings:
    """运行配置(从环境变量读取)。"""

    def __init__(self) -> None:
        load_dotenv()
        # 凭证
        self.qianfan_api_key = os.getenv("QIANFAN_API_KEY", "")
        self.tencent_secret_id = os.getenv("TENCENT_SECRET_ID", "")
        self.tencent_secret_key = os.getenv("TENCENT_SECRET_KEY", "")
        self.serpapi_api_key = os.getenv("SERPAPI_API_KEY", "")
        # 检索参数
        self.default_top_k = int(os.getenv("SEARCH_TOP_K", "10"))
        self.per_provider_k = int(os.getenv("SEARCH_PER_PROVIDER_K", "10"))
        self.provider_timeout = int(os.getenv("SEARCH_PROVIDER_TIMEOUT", "15"))
        # 重排(默认启用:走 siliconflow 云端 cross-encoder 重排,质量更高但慢数秒;
        #  设 RERANK_ENABLED=false 可关闭,改走本地 RRF 多源融合,毫秒级)
        self.rerank_enabled = os.getenv("RERANK_ENABLED", "true").lower() == "true"
        self.rerank_backend = os.getenv("RERANK_BACKEND", "siliconflow")  # siliconflow | bge | flashrank | none
        self.rerank_model = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        self.rerank_device = os.getenv("RERANK_DEVICE", "") or None  # None=自动(GPU 优先)
        self.rerank_cache_dir = os.getenv("RERANK_CACHE_DIR", "/data/.flashrank")
        # SiliconFlow API reranker
        self.siliconflow_api_key = os.getenv("SILICONFLOW_API_KEY", "")
        self.siliconflow_base_url = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        # 分块
        self.chunk_max_chars = int(os.getenv("CHUNK_MAX_CHARS", "400"))
        self.chunk_overlap = int(os.getenv("CHUNK_OVERLAP", "50"))
        # 阈值过滤
        self.rerank_threshold = float(os.getenv("RERANK_THRESHOLD", "0.3"))
        # L0 查询改写
        self.rewrite_enabled = os.getenv("REWRITE_ENABLED", "false").lower() == "true"
        self.rewrite_model = os.getenv("REWRITE_MODEL", "Qwen/Qwen2.5-7B-Instruct")
        self.rewrite_cache_size = int(os.getenv("REWRITE_CACHE_SIZE", "512"))
        # Phase 1 陈述校验:auto=有 SiliconFlow key 时做模型蕴含判断,否则保守规则。
        self.trust_verify_backend = os.getenv("TRUST_VERIFY_BACKEND", "auto").lower()
        self.trust_verify_model = os.getenv("TRUST_VERIFY_MODEL", self.rewrite_model)
        self.trust_verify_timeout = int(os.getenv("TRUST_VERIFY_TIMEOUT", "15"))
        self.trust_verify_max_claims = int(os.getenv("TRUST_VERIFY_MAX_CLAIMS", "20"))
        self.trust_verify_max_evidence = int(os.getenv("TRUST_VERIFY_MAX_EVIDENCE", "5"))
        # 辅助信号融合(评测证明对通用查询是负优化,默认关闭;时效场景可手动开)
        self.fusion_enabled = os.getenv("FUSION_ENABLED", "false").lower() == "true"
        self.fusion_alpha = float(os.getenv("FUSION_ALPHA", "0.7"))    # 文本相关性
        self.fusion_beta = float(os.getenv("FUSION_BETA", "0.15"))     # 新鲜度
        self.fusion_gamma = float(os.getenv("FUSION_GAMMA", "0.10"))   # 来源权威度
        self.fusion_delta = float(os.getenv("FUSION_DELTA", "0.05"))   # 源内排名
        # OpenAlex 学术检索(数据源 = 本地 Chukonu 检索系统的 ES;独立于 web 搜索)
        self.openalex_api_url = os.getenv("OPENALEX_API_URL", "http://localhost:9001")  # Chukonu 服务基址
        self.openalex_api_key = os.getenv("OPENALEX_API_KEY", "")  # 可选 X-API-Key(服务开放时留空)
        self.openalex_enabled = os.getenv("OPENALEX_ENABLED", "false").lower() == "true"
        self.openalex_mailto = os.getenv("OPENALEX_MAILTO", "search-engine@example.com")
        self.openalex_topic_filter = os.getenv("OPENALEX_TOPIC_FILTER", "")  # 保留(当前后端未用)
        self.openalex_per_page = int(os.getenv("OPENALEX_PER_PAGE", "25"))
        self.openalex_academic_detect = os.getenv("OPENALEX_ACADEMIC_DETECT", "true").lower() == "true"
        # 学术 query 改写:把自然语言问句提取为论文标题/英文检索词喂给 OpenAlex(解决召回空)
        self.openalex_query_rewrite = os.getenv("OPENALEX_QUERY_REWRITE", "true").lower() == "true"
        # OpenAlex PDF 正文富化：默认关闭，只在请求 include_pdf_text=true 时对重排后前 N 条执行。
        self.openalex_pdf_text_mode = os.getenv("OPENALEX_PDF_TEXT_MODE", "sync")  # cached | sync
        self.openalex_pdf_max_results = int(os.getenv("OPENALEX_PDF_MAX_RESULTS", "2"))
        self.openalex_pdf_max_chars = int(os.getenv("OPENALEX_PDF_MAX_CHARS", "8000"))
        self.openalex_pdf_timeout_ms = int(os.getenv("OPENALEX_PDF_TIMEOUT_MS", "10000"))
        self.openalex_pdf_total_budget_ms = int(os.getenv("OPENALEX_PDF_TOTAL_BUDGET_MS", "15000"))
        # 专利检索(houdutech 只读 ES;独立于 web 搜索,缺 URL 则静默关闭)
        self.patent_es_url = os.getenv("PATENT_ES_URL", "")  # https://search.houdutech.cn:9243
        self.patent_es_index = os.getenv("PATENT_ES_INDEX", "epo_docdb_read")
        self.patent_es_enabled = os.getenv("PATENT_ES_ENABLED", "false").lower() == "true"
        self.patent_es_verify_tls = os.getenv("PATENT_ES_VERIFY_TLS", "true").lower() == "true"
        self.patent_es_per_page = int(os.getenv("PATENT_ES_PER_PAGE", "25"))
        self.patent_detect = os.getenv("PATENT_DETECT", "true").lower() == "true"  # L0 专利意图自动识别
        # 搜索结果缓存(provider 召回级:避免重复调用搜索源 API;时效查询不缓存)
        self.cache_enabled = os.getenv("CACHE_ENABLED", "true").lower() == "true"
        self.cache_backend = os.getenv("CACHE_BACKEND", "memory")  # memory(预留 redis)
        self.cache_ttl = int(os.getenv("CACHE_TTL", "21600"))      # 非时效结果 TTL,默认 6 小时
        self.cache_max_size = int(os.getenv("CACHE_MAX_SIZE", "512"))  # 进程内缓存条目上限
        # API 鉴权:配了 API_AUTH_TOKEN(可逗号分隔多个)即对 /search 与 /mcp 强制
        #  Bearer/X-API-Key 校验;留空=不鉴权(本地开发默认)
        self.api_auth_token = os.getenv("API_AUTH_TOKEN", "")

    @property
    def auth_tokens(self) -> set:
        """有效 token 集合(API_AUTH_TOKEN,逗号分隔)。"""
        return {t.strip() for t in self.api_auth_token.split(",") if t.strip()}

    @property
    def auth_enabled(self) -> bool:
        """是否启用 API 鉴权(配了至少一个 token)。"""
        return bool(self.auth_tokens)

    @property
    def enabled_providers(self) -> List[str]:
        """根据已配置的凭证自动决定启用哪些 web 搜索源(不含学术源)。"""
        names: List[str] = []
        if self.tencent_secret_id and self.tencent_secret_key:
            names.append("tencent")
        if self.qianfan_api_key:
            names.append("baidu")
        if self.serpapi_api_key:
            names.append("serpapi")
        return names

    @property
    def academic_enabled(self) -> bool:
        """学术检索(OpenAlex 数据,经 Chukonu 服务)是否启用:配了服务基址即启用。"""
        return bool(self.openalex_api_url) or self.openalex_enabled

    @property
    def patent_enabled(self) -> bool:
        """专利检索(ES)是否启用:配了 PATENT_ES_URL 或显式 PATENT_ES_ENABLED=true。"""
        return bool(self.patent_es_url) or self.patent_es_enabled


settings = Settings()
