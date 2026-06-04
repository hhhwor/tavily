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
        # 检索参数
        self.default_top_k = int(os.getenv("SEARCH_TOP_K", "10"))
        self.per_provider_k = int(os.getenv("SEARCH_PER_PROVIDER_K", "10"))
        self.provider_timeout = int(os.getenv("SEARCH_PROVIDER_TIMEOUT", "15"))
        # 重排
        self.rerank_enabled = os.getenv("RERANK_ENABLED", "true").lower() == "true"
        self.rerank_backend = os.getenv("RERANK_BACKEND", "bge")  # bge | flashrank | none
        self.rerank_model = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        self.rerank_device = os.getenv("RERANK_DEVICE", "") or None  # None=自动(GPU 优先)
        self.rerank_cache_dir = os.getenv("RERANK_CACHE_DIR", "/data/.flashrank")

    @property
    def enabled_providers(self) -> List[str]:
        """根据已配置的凭证自动决定启用哪些搜索源。"""
        names: List[str] = []
        if self.tencent_secret_id and self.tencent_secret_key:
            names.append("tencent")
        if self.qianfan_api_key:
            names.append("baidu")
        return names


settings = Settings()
