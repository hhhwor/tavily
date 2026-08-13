"""不可变运行配置；环境读取只由 composition root 显式触发。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import FrozenSet, Mapping, Optional, Tuple

from src.pipeline.ranking_options import resolve_ranking_options


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read_dotenv(path: str) -> dict[str, str]:
    """读取简单 KEY=VALUE 文件，但不修改 ``os.environ``。"""
    values: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _environment(
    environ: Optional[Mapping[str, str]],
    dotenv_path: Optional[str],
) -> dict[str, str]:
    """构造配置快照。

    ``environ`` 非空时把它视为完整、可复现的输入；只有调用方未传映射时才合并
    项目 ``.env`` 与进程环境。显式传 ``dotenv_path`` 时则先读该文件再覆盖映射。
    """
    if environ is None:
        path = dotenv_path if dotenv_path is not None else os.path.join(_project_root(), ".env")
        values = _read_dotenv(path)
        values.update(os.environ)
        return values
    values = _read_dotenv(dotenv_path) if dotenv_path else {}
    values.update(environ)
    return values


def _optional_bool(env: Mapping[str, str], name: str) -> Optional[bool]:
    value = env.get(name)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} 仅支持 true / false，收到 {value!r}")


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _optional_bool(env, name)
    return default if value is None else value


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数，收到 {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} 必须 >= {minimum}，收到 {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字，收到 {raw!r}") from exc


def _csv(env: Mapping[str, str], name: str) -> Tuple[str, ...]:
    return tuple(item.strip() for item in env.get(name, "").split(",") if item.strip())


def _mcp_mode(value: str) -> str:
    mode = (value or "auto").strip().lower()
    if mode not in {"auto", "true", "false"}:
        raise ValueError(f"MCP_ENABLED 仅支持 auto / true / false，收到 {value!r}")
    return mode


@dataclass(frozen=True)
class Settings:
    """进程配置快照。

    直接构造 ``Settings()`` 得到无凭证的安全默认值；生产启动使用
    ``Settings.from_env()``。冻结对象避免请求期间修改全局配置。
    """

    qianfan_api_key: str = field(default="", repr=False)
    tencent_secret_id: str = field(default="", repr=False)
    tencent_secret_key: str = field(default="", repr=False)
    doubao_api_key: str = field(default="", repr=False)
    doubao_uvx_path: str = ""
    aliyun_access_key_id: str = field(default="", repr=False)
    aliyun_access_key_secret: str = field(default="", repr=False)
    aliyun_web_search_enabled: bool = True
    aliyun_web_search_type: str = "pro"
    aliyun_web_search_region: str = "global"
    serpapi_api_key: str = field(default="", repr=False)
    serpapi_enabled: bool = False
    fy_law_mcp_url: str = "https://api.cjbdi.com:8443/354347/mcp_law_service"
    fy_law_mcp_token: str = field(default="", repr=False)
    fy_law_mcp_token_file: str = ""
    fy_law_mcp_enabled: bool = False
    fy_law_mcp_detect: bool = True

    default_top_k: int = 10
    per_provider_k: int = 10
    provider_timeout: int = 15
    search_deadline_ms: int = 30000
    state_db_path: str = "/tmp/chukonu-state.sqlite3"
    search_seed_ttl_seconds: int = 86400
    research_max_workers: int = 4
    research_queue_capacity: int = 32
    research_queue_ttl_ms: int = 120000
    research_queue_retry_after_seconds: int = 1
    research_recall_max_workers: int = 4
    research_ranking_max_workers: int = 2
    research_synthesis_enabled: bool = False
    research_synthesis_model: str = "Qwen/Qwen2.5-7B-Instruct"
    research_synthesis_timeout: int = 20
    research_artifact_retention_seconds: int = 604800

    ranking_profile: str = "quality"
    rerank_backend: str = "siliconflow"
    rerank_model: str = "Qwen/Qwen3-Reranker-0.6B"
    rerank_device: Optional[str] = None
    rerank_cache_dir: str = "/data/.flashrank"
    rerank_threshold: float = 0.3
    rerank_threshold_mode: str = "prefer"
    ranking_warnings: Tuple[str, ...] = ()

    siliconflow_api_key: str = field(default="", repr=False)
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    intent_classifier_enabled: bool = False
    intent_classifier_backend: str = "embedding"
    intent_classifier_model: str = "Qwen/Qwen3-Embedding-0.6B"
    intent_classifier_timeout: int = 8
    intent_classifier_cache_size: int = 1024
    intent_classifier_cache_ttl: int = 3600
    intent_classifier_min_confidence: float = 0.70
    intent_embedding_academic_threshold: float = 0.60
    intent_embedding_patent_threshold: float = 0.53
    intent_embedding_legal_threshold: float = 0.61
    intent_embedding_general_margin: float = 0.03
    intent_embedding_confidence_scale: float = 0.02
    chunk_max_chars: int = 400
    chunk_overlap: int = 50

    rewrite_enabled: bool = False
    rewrite_model: str = "Qwen/Qwen2.5-7B-Instruct"
    rewrite_cache_size: int = 512

    trust_verify_backend: str = "auto"
    trust_verify_model: str = "Qwen/Qwen3-8B"
    trust_verify_timeout: int = 15
    trust_verify_max_claims: int = 20
    trust_verify_max_evidence: int = 5

    fusion_alpha: float = 0.7
    fusion_beta: float = 0.15
    fusion_gamma: float = 0.10
    fusion_delta: float = 0.05

    openalex_api_url: str = "http://localhost:9001"
    openalex_api_key: str = field(default="", repr=False)
    openalex_enabled: bool = True
    openalex_mailto: str = "search-engine@example.com"
    openalex_topic_filter: str = ""
    openalex_per_page: int = 25
    openalex_academic_detect: bool = True
    openalex_query_rewrite: bool = True
    openalex_pdf_text_mode: str = "sync"
    openalex_pdf_max_results: int = 2
    openalex_pdf_max_chars: int = 8000
    openalex_pdf_timeout_ms: int = 10000
    openalex_pdf_total_budget_ms: int = 15000

    patent_es_url: str = ""
    patent_es_index: str = "epo_docdb_read"
    patent_es_enabled: bool = False
    patent_es_verify_tls: bool = True
    patent_es_per_page: int = 25
    patent_detect: bool = True
    patent_fulltext_url: str = ""
    patent_fulltext_index: str = "epo_fulltext_read"
    patent_fulltext_enabled: bool = False
    patent_fulltext_verify_tls: bool = True

    cache_enabled: bool = True
    cache_backend: str = "memory"
    cache_ttl: int = 21600
    cache_max_size: int = 512
    # Recall keeps the legacy EXECUTOR_MAX_WORKERS budget. Ranking and PDF use
    # dedicated pools so a slow external workload cannot consume recall slots.
    executor_max_workers: int = 16
    ranking_executor_max_workers: int = 4
    pdf_executor_max_workers: int = 4
    resilience_max_attempts: int = 2
    resilience_backoff_base_ms: int = 100
    resilience_backoff_max_ms: int = 1000
    circuit_failure_threshold: int = 3
    circuit_open_seconds: int = 30

    api_auth_token: str = field(default="", repr=False)
    mcp_mode: str = "auto"
    mcp_dns_rebinding_protection: bool = True
    mcp_allowed_hosts: Tuple[str, ...] = ()
    mcp_allowed_origins: Tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        *,
        dotenv_path: Optional[str] = None,
    ) -> "Settings":
        env = _environment(environ, dotenv_path)

        if bool(env.get("TENCENT_SECRET_ID")) != bool(env.get("TENCENT_SECRET_KEY")):
            raise ValueError(
                "TENCENT_SECRET_ID 与 TENCENT_SECRET_KEY 必须同时配置"
            )
        serpapi_enabled = _bool(env, "SERPAPI_ENABLED", False)
        if serpapi_enabled and not env.get("SERPAPI_API_KEY"):
            raise ValueError(
                "SERPAPI_ENABLED=true 时必须配置 SERPAPI_API_KEY"
            )
        fy_law_mcp_enabled = _bool(env, "FY_LAW_MCP_ENABLED", False)
        fy_law_mcp_url = env.get(
            "FY_LAW_MCP_URL",
            "https://api.cjbdi.com:8443/354347/mcp_law_service",
        ).strip()
        fy_law_mcp_token = env.get("FY_LAW_MCP_TOKEN", "").strip()
        fy_law_mcp_token_file = env.get("FY_LAW_MCP_TOKEN_FILE", "").strip()
        if fy_law_mcp_enabled and not fy_law_mcp_url:
            raise ValueError("FY_LAW_MCP_ENABLED=true 时必须配置 FY_LAW_MCP_URL")
        if fy_law_mcp_enabled and not (
            fy_law_mcp_token or fy_law_mcp_token_file
        ):
            raise ValueError(
                "FY_LAW_MCP_ENABLED=true 时必须配置 FY_LAW_MCP_TOKEN 或 "
                "FY_LAW_MCP_TOKEN_FILE"
            )
        aliyun_id = env.get("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
        aliyun_secret = env.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
        if bool(aliyun_id) != bool(aliyun_secret):
            raise ValueError(
                "ALIBABA_CLOUD_ACCESS_KEY_ID 与 "
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET 必须同时配置"
            )
        aliyun_flag = _optional_bool(env, "ALIYUN_WEB_SEARCH_ENABLED")
        aliyun_enabled = (
            bool(aliyun_id and aliyun_secret)
            if aliyun_flag is None
            else aliyun_flag
        )
        if aliyun_enabled and not aliyun_id:
            raise ValueError(
                "ALIYUN_WEB_SEARCH_ENABLED=true 时必须配置阿里云 AccessKey"
            )
        aliyun_type = env.get("ALIYUN_WEB_SEARCH_TYPE", "pro").strip().lower()
        if aliyun_type not in {"pro", "lite"}:
            raise ValueError("ALIYUN_WEB_SEARCH_TYPE 仅支持 pro / lite")
        aliyun_region = env.get(
            "ALIYUN_WEB_SEARCH_REGION", "global"
        ).strip().lower()
        if aliyun_region not in {"global", "mainland_china"}:
            raise ValueError(
                "ALIYUN_WEB_SEARCH_REGION 仅支持 global / mainland_china"
            )

        rerank_backend = env.get("RERANK_BACKEND", "siliconflow")
        ranking = resolve_ranking_options(
            default_profile="quality",
            default_threshold=0.3,
            default_threshold_mode="prefer",
            ranking_profile=env.get("RANKING_PROFILE"),
            rerank_enabled=_optional_bool(env, "RERANK_ENABLED"),
            fusion_enabled=_optional_bool(env, "FUSION_ENABLED"),
            rerank_backend=rerank_backend,
            rerank_threshold=env.get("RERANK_THRESHOLD"),
            rerank_threshold_mode=env.get("RERANK_THRESHOLD_MODE"),
        )

        openalex_url = env.get("OPENALEX_API_URL", "http://localhost:9001").strip()
        openalex_flag = _optional_bool(env, "OPENALEX_ENABLED")
        openalex_enabled = bool(openalex_url) if openalex_flag is None else openalex_flag
        if openalex_enabled and not openalex_url:
            raise ValueError("OPENALEX_ENABLED=true 时必须配置 OPENALEX_API_URL")

        patent_url = env.get("PATENT_ES_URL", "").strip()
        patent_flag = _optional_bool(env, "PATENT_ES_ENABLED")
        patent_enabled = bool(patent_url) if patent_flag is None else patent_flag
        if patent_enabled and not patent_url:
            raise ValueError("PATENT_ES_ENABLED=true 时必须配置 PATENT_ES_URL")
        patent_fulltext_url = env.get("PATENT_FULLTEXT_URL", "").strip()
        patent_fulltext_flag = _optional_bool(env, "PATENT_FULLTEXT_ENABLED")
        patent_fulltext_enabled = (
            bool(patent_fulltext_url)
            if patent_fulltext_flag is None else patent_fulltext_flag
        )
        patent_fulltext_index = env.get(
            "PATENT_FULLTEXT_INDEX", "epo_fulltext_read"
        ).strip()
        if patent_fulltext_enabled and not patent_fulltext_url:
            raise ValueError(
                "PATENT_FULLTEXT_ENABLED=true 时必须配置 PATENT_FULLTEXT_URL"
            )
        if patent_fulltext_enabled and not patent_fulltext_index:
            raise ValueError(
                "PATENT_FULLTEXT_ENABLED=true 时必须配置 PATENT_FULLTEXT_INDEX"
            )

        rewrite_model = env.get("REWRITE_MODEL", "Qwen/Qwen2.5-7B-Instruct")
        intent_classifier_backend = env.get(
            "INTENT_CLASSIFIER_BACKEND", "embedding"
        ).strip().lower()
        if intent_classifier_backend not in {"embedding", "chat"}:
            raise ValueError(
                "INTENT_CLASSIFIER_BACKEND 仅支持 embedding / chat"
            )
        intent_classifier_enabled = _bool(
            env, "INTENT_CLASSIFIER_ENABLED", False
        )
        if intent_classifier_enabled and not env.get("SILICONFLOW_API_KEY"):
            raise ValueError(
                "INTENT_CLASSIFIER_ENABLED=true 时必须配置 SILICONFLOW_API_KEY"
            )
        intent_classifier_min_confidence = _float(
            env, "INTENT_CLASSIFIER_MIN_CONFIDENCE", 0.70
        )
        if not 0.0 <= intent_classifier_min_confidence <= 1.0:
            raise ValueError(
                "INTENT_CLASSIFIER_MIN_CONFIDENCE 必须在 0 到 1 之间"
            )
        intent_embedding_thresholds = {
            name: _float(env, variable, default)
            for name, variable, default in (
                ("academic", "INTENT_EMBEDDING_ACADEMIC_THRESHOLD", 0.60),
                ("patent", "INTENT_EMBEDDING_PATENT_THRESHOLD", 0.53),
                ("legal", "INTENT_EMBEDDING_LEGAL_THRESHOLD", 0.61),
            )
        }
        if any(
            not 0.0 <= value <= 1.0
            for value in intent_embedding_thresholds.values()
        ):
            raise ValueError("INTENT_EMBEDDING_*_THRESHOLD 必须在 0 到 1 之间")
        intent_embedding_general_margin = _float(
            env, "INTENT_EMBEDDING_GENERAL_MARGIN", 0.03
        )
        if not 0.0 <= intent_embedding_general_margin <= 1.0:
            raise ValueError(
                "INTENT_EMBEDDING_GENERAL_MARGIN 必须在 0 到 1 之间"
            )
        intent_embedding_confidence_scale = _float(
            env, "INTENT_EMBEDDING_CONFIDENCE_SCALE", 0.02
        )
        if intent_embedding_confidence_scale <= 0.0:
            raise ValueError("INTENT_EMBEDDING_CONFIDENCE_SCALE 必须大于 0")
        synthesis_enabled = _bool(
            env, "RESEARCH_SYNTHESIS_ENABLED", False
        )
        if synthesis_enabled and not env.get("SILICONFLOW_API_KEY"):
            raise ValueError(
                "RESEARCH_SYNTHESIS_ENABLED=true 时必须配置 SILICONFLOW_API_KEY"
            )
        return cls(
            qianfan_api_key=env.get("QIANFAN_API_KEY", ""),
            tencent_secret_id=env.get("TENCENT_SECRET_ID", ""),
            tencent_secret_key=env.get("TENCENT_SECRET_KEY", ""),
            doubao_api_key=(
                env.get("DOUBAO_API_KEY")
                or env.get("ASK_ECHO_SEARCH_INFINITY_API_KEY", "")
            ),
            doubao_uvx_path=env.get("DOUBAO_UVX_PATH", ""),
            aliyun_access_key_id=aliyun_id,
            aliyun_access_key_secret=aliyun_secret,
            aliyun_web_search_enabled=aliyun_enabled,
            aliyun_web_search_type=aliyun_type,
            aliyun_web_search_region=aliyun_region,
            serpapi_api_key=env.get("SERPAPI_API_KEY", ""),
            serpapi_enabled=serpapi_enabled,
            fy_law_mcp_url=fy_law_mcp_url,
            fy_law_mcp_token=fy_law_mcp_token,
            fy_law_mcp_token_file=fy_law_mcp_token_file,
            fy_law_mcp_enabled=fy_law_mcp_enabled,
            fy_law_mcp_detect=_bool(env, "FY_LAW_MCP_DETECT", True),
            default_top_k=_int(env, "SEARCH_TOP_K", 10, minimum=1),
            per_provider_k=_int(env, "SEARCH_PER_PROVIDER_K", 10, minimum=1),
            provider_timeout=_int(env, "SEARCH_PROVIDER_TIMEOUT", 15, minimum=1),
            search_deadline_ms=_int(env, "SEARCH_DEADLINE_MS", 30000, minimum=1),
            state_db_path=env.get(
                "STATE_DB_PATH", "/tmp/chukonu-state.sqlite3"
            ),
            search_seed_ttl_seconds=_int(
                env, "SEARCH_SEED_TTL_SECONDS", 86400, minimum=1
            ),
            research_max_workers=_int(
                env, "RESEARCH_MAX_WORKERS", 4, minimum=1
            ),
            research_queue_capacity=_int(
                env, "RESEARCH_QUEUE_CAPACITY", 32, minimum=0
            ),
            research_queue_ttl_ms=_int(
                env, "RESEARCH_QUEUE_TTL_MS", 120000, minimum=1
            ),
            research_queue_retry_after_seconds=_int(
                env, "RESEARCH_QUEUE_RETRY_AFTER_SECONDS", 1, minimum=1
            ),
            research_recall_max_workers=_int(
                env, "RESEARCH_RECALL_MAX_WORKERS", 4, minimum=1
            ),
            research_ranking_max_workers=_int(
                env, "RESEARCH_RANKING_MAX_WORKERS", 2, minimum=1
            ),
            research_synthesis_enabled=synthesis_enabled,
            research_synthesis_model=env.get(
                "RESEARCH_SYNTHESIS_MODEL", "Qwen/Qwen2.5-7B-Instruct"
            ),
            research_synthesis_timeout=_int(
                env, "RESEARCH_SYNTHESIS_TIMEOUT", 20, minimum=1
            ),
            research_artifact_retention_seconds=_int(
                env,
                "RESEARCH_ARTIFACT_RETENTION_SECONDS",
                604800,
                minimum=1,
            ),
            ranking_profile=ranking.profile,
            rerank_backend=rerank_backend,
            rerank_model=env.get("RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B"),
            rerank_device=env.get("RERANK_DEVICE", "") or None,
            rerank_cache_dir=env.get("RERANK_CACHE_DIR", "/data/.flashrank"),
            rerank_threshold=ranking.threshold,
            rerank_threshold_mode=ranking.threshold_mode,
            ranking_warnings=ranking.warnings,
            siliconflow_api_key=env.get("SILICONFLOW_API_KEY", ""),
            siliconflow_base_url=env.get(
                "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
            ),
            intent_classifier_enabled=intent_classifier_enabled,
            intent_classifier_backend=intent_classifier_backend,
            intent_classifier_model=env.get(
                "INTENT_CLASSIFIER_MODEL",
                (
                    "Qwen/Qwen3-Embedding-0.6B"
                    if intent_classifier_backend == "embedding"
                    else "Qwen/Qwen3-8B"
                ),
            ),
            intent_classifier_timeout=_int(
                env, "INTENT_CLASSIFIER_TIMEOUT", 8, minimum=1
            ),
            intent_classifier_cache_size=_int(
                env, "INTENT_CLASSIFIER_CACHE_SIZE", 1024, minimum=1
            ),
            intent_classifier_cache_ttl=_int(
                env, "INTENT_CLASSIFIER_CACHE_TTL", 3600, minimum=1
            ),
            intent_classifier_min_confidence=intent_classifier_min_confidence,
            intent_embedding_academic_threshold=(
                intent_embedding_thresholds["academic"]
            ),
            intent_embedding_patent_threshold=(
                intent_embedding_thresholds["patent"]
            ),
            intent_embedding_legal_threshold=intent_embedding_thresholds["legal"],
            intent_embedding_general_margin=intent_embedding_general_margin,
            intent_embedding_confidence_scale=intent_embedding_confidence_scale,
            chunk_max_chars=_int(env, "CHUNK_MAX_CHARS", 400, minimum=1),
            chunk_overlap=_int(env, "CHUNK_OVERLAP", 50, minimum=0),
            rewrite_enabled=_bool(env, "REWRITE_ENABLED", False),
            rewrite_model=rewrite_model,
            rewrite_cache_size=_int(env, "REWRITE_CACHE_SIZE", 512, minimum=1),
            trust_verify_backend=env.get("TRUST_VERIFY_BACKEND", "auto").lower(),
            trust_verify_model=env.get("TRUST_VERIFY_MODEL", "Qwen/Qwen3-8B"),
            trust_verify_timeout=_int(env, "TRUST_VERIFY_TIMEOUT", 15, minimum=1),
            trust_verify_max_claims=_int(env, "TRUST_VERIFY_MAX_CLAIMS", 20, minimum=1),
            trust_verify_max_evidence=_int(env, "TRUST_VERIFY_MAX_EVIDENCE", 5, minimum=1),
            fusion_alpha=_float(env, "FUSION_ALPHA", 0.7),
            fusion_beta=_float(env, "FUSION_BETA", 0.15),
            fusion_gamma=_float(env, "FUSION_GAMMA", 0.10),
            fusion_delta=_float(env, "FUSION_DELTA", 0.05),
            openalex_api_url=openalex_url,
            openalex_api_key=env.get("OPENALEX_API_KEY", ""),
            openalex_enabled=openalex_enabled,
            openalex_mailto=env.get("OPENALEX_MAILTO", "search-engine@example.com"),
            openalex_topic_filter=env.get("OPENALEX_TOPIC_FILTER", ""),
            openalex_per_page=_int(env, "OPENALEX_PER_PAGE", 25, minimum=1),
            openalex_academic_detect=_bool(env, "OPENALEX_ACADEMIC_DETECT", True),
            openalex_query_rewrite=_bool(env, "OPENALEX_QUERY_REWRITE", True),
            openalex_pdf_text_mode=env.get("OPENALEX_PDF_TEXT_MODE", "sync"),
            openalex_pdf_max_results=_int(env, "OPENALEX_PDF_MAX_RESULTS", 2, minimum=0),
            openalex_pdf_max_chars=_int(env, "OPENALEX_PDF_MAX_CHARS", 8000, minimum=1),
            openalex_pdf_timeout_ms=_int(env, "OPENALEX_PDF_TIMEOUT_MS", 10000, minimum=1),
            openalex_pdf_total_budget_ms=_int(
                env, "OPENALEX_PDF_TOTAL_BUDGET_MS", 15000, minimum=1
            ),
            patent_es_url=patent_url,
            patent_es_index=env.get("PATENT_ES_INDEX", "epo_docdb_read"),
            patent_es_enabled=patent_enabled,
            patent_es_verify_tls=_bool(env, "PATENT_ES_VERIFY_TLS", True),
            patent_es_per_page=_int(env, "PATENT_ES_PER_PAGE", 25, minimum=1),
            patent_detect=_bool(env, "PATENT_DETECT", True),
            patent_fulltext_url=patent_fulltext_url,
            patent_fulltext_index=patent_fulltext_index,
            patent_fulltext_enabled=patent_fulltext_enabled,
            patent_fulltext_verify_tls=_bool(
                env, "PATENT_FULLTEXT_VERIFY_TLS", True
            ),
            cache_enabled=_bool(env, "CACHE_ENABLED", True),
            cache_backend=env.get("CACHE_BACKEND", "memory"),
            cache_ttl=_int(env, "CACHE_TTL", 21600, minimum=0),
            cache_max_size=_int(env, "CACHE_MAX_SIZE", 512, minimum=1),
            executor_max_workers=_int(env, "EXECUTOR_MAX_WORKERS", 16, minimum=1),
            ranking_executor_max_workers=_int(
                env, "RANKING_EXECUTOR_MAX_WORKERS", 4, minimum=1
            ),
            pdf_executor_max_workers=_int(
                env, "PDF_EXECUTOR_MAX_WORKERS", 4, minimum=1
            ),
            resilience_max_attempts=_int(
                env, "RESILIENCE_MAX_ATTEMPTS", 2, minimum=1
            ),
            resilience_backoff_base_ms=_int(
                env, "RESILIENCE_BACKOFF_BASE_MS", 100, minimum=0
            ),
            resilience_backoff_max_ms=_int(
                env, "RESILIENCE_BACKOFF_MAX_MS", 1000, minimum=0
            ),
            circuit_failure_threshold=_int(
                env, "CIRCUIT_FAILURE_THRESHOLD", 3, minimum=1
            ),
            circuit_open_seconds=_int(
                env, "CIRCUIT_OPEN_SECONDS", 30, minimum=1
            ),
            api_auth_token=env.get("API_AUTH_TOKEN", ""),
            mcp_mode=_mcp_mode(env.get("MCP_ENABLED", "auto")),
            mcp_dns_rebinding_protection=_bool(
                env, "MCP_DNS_REBINDING_PROTECTION", True
            ),
            mcp_allowed_hosts=_csv(env, "MCP_ALLOWED_HOSTS"),
            mcp_allowed_origins=_csv(env, "MCP_ALLOWED_ORIGINS"),
        )

    @property
    def rerank_enabled(self) -> bool:
        return self.ranking_profile != "fast"

    @property
    def fusion_enabled(self) -> bool:
        return self.ranking_profile == "quality"

    @property
    def auth_tokens(self) -> FrozenSet[str]:
        return frozenset(item.strip() for item in self.api_auth_token.split(",") if item.strip())

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_tokens)

    @property
    def mcp_enabled(self) -> bool:
        return self.mcp_mode != "false"

    @property
    def mcp_required(self) -> bool:
        return self.mcp_mode == "true"

    @property
    def enabled_providers(self) -> Tuple[str, ...]:
        names = []
        if self.tencent_secret_id and self.tencent_secret_key:
            names.append("tencent")
        if self.qianfan_api_key:
            names.append("baidu")
        if self.doubao_api_key:
            names.append("doubao")
        if (
            self.aliyun_web_search_enabled
            and self.aliyun_access_key_id
            and self.aliyun_access_key_secret
        ):
            names.append("aliyun")
        if self.serpapi_enabled and self.serpapi_api_key:
            names.append("serpapi")
        if self.fy_law_mcp_enabled:
            names.append("fy_law_mcp")
        return tuple(names)

    @property
    def academic_enabled(self) -> bool:
        return self.openalex_enabled and bool(self.openalex_api_url)

    @property
    def patent_enabled(self) -> bool:
        return self.patent_es_enabled and bool(self.patent_es_url)
