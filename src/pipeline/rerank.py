"""重排序:cross-encoder 段落级重排 + sigmoid 归一化 + 阈值过滤。

段落级重排:
  1. 文档先切 chunk(src.pipeline.chunk)
  2. 逐块与 query 组 pair 交给 cross-encoder 打分
  3. 每文档取 chunk 最高分(max-pooling)
  4. sigmoid 归一化到 0-1
  5. 低于阈值的结果被过滤

回退:reranker 不可用时自动降级为 NoOpReranker,保证管线始终可跑。
"""
from __future__ import annotations

import math
import re as _re
import requests as _requests
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.models import SearchResult
from src.pipeline.chunk import chunk_text


class Reranker(ABC):
    name: str = "base"

    @abstractmethod
    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        raise NotImplementedError


class NoOpReranker(Reranker):
    """不重排:按来源原始分(若有)降序,否则保持原顺序。"""

    name = "noop"

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        ordered = sorted(
            results, key=lambda r: (r.score is not None, r.score or 0.0), reverse=True
        )
        return ordered[:top_k]


def sigmoid_normalize(scores: List[float], temperature: float = 1.0) -> List[float]:
    """将任意范围的分数通过 sigmoid 归一化到 0-1。

    temperature > 1 使分布更尖锐(拉开差距),< 1 更平缓。
    """
    return [1.0 / (1.0 + math.exp(-s * temperature)) for s in scores]


class FlashRankReranker(Reranker):
    """FlashRank cross-encoder 段落级重排。"""

    name = "flashrank"

    def __init__(
        self,
        model_name: str,
        cache_dir: str,
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ):
        from flashrank import Ranker  # 延迟导入

        self._ranker = Ranker(model_name=model_name, cache_dir=cache_dir)
        self.name = f"flashrank:{model_name}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        from flashrank import RerankRequest

        if not results:
            return []

        # 1) 构建 (result_idx, chunk_text) pairs
        pairs: List[tuple[int, str]] = []
        for i, r in enumerate(results):
            chunks = chunk_text(
                r.text_for_rerank(), self._chunk_max_chars, self._chunk_overlap
            )
            for c in chunks:
                pairs.append((i, c))

        if not pairs:
            return results[:top_k]

        # 2) 批量打分
        passages = [{"id": j, "text": t} for j, (_, t) in enumerate(pairs)]
        scored = self._ranker.rerank(RerankRequest(query=query, passages=passages))

        # 3) max-pooling:每文档取 chunk 最高分
        doc_scores: dict[int, float] = {}
        for item in scored:
            doc_idx = pairs[item["id"]][0]
            s = float(item["score"])
            if doc_idx not in doc_scores or s > doc_scores[doc_idx]:
                doc_scores[doc_idx] = s

        # 4) sigmoid 归一化 + 赋值
        raw = [doc_scores.get(i, 0.0) for i in range(len(results))]
        normed = sigmoid_normalize(raw)
        for r, s in zip(results, normed):
            r.rerank_score = s

        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return ranked[:top_k]


class BGEReranker(Reranker):
    """BGE-Reranker-v2-m3 cross-encoder 段落级重排(多语言,中文强;需 torch)。"""

    name = "bge"

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        max_length: int = 512,
        device: Optional[str] = None,
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ):
        from sentence_transformers import CrossEncoder  # 延迟导入

        kwargs = {"max_length": max_length}
        if device:
            kwargs["device"] = device
        self._model = CrossEncoder(model_name, **kwargs)
        self.name = f"bge:{model_name.split('/')[-1]}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        if not results:
            return []

        # 1) 构建 (result_idx, chunk_text) pairs
        pairs: List[tuple[int, str]] = []
        for i, r in enumerate(results):
            chunks = chunk_text(
                r.text_for_rerank(), self._chunk_max_chars, self._chunk_overlap
            )
            for c in chunks:
                pairs.append((i, c))

        if not pairs:
            return results[:top_k]

        # 2) 批量打分
        predict_pairs = [(query, t) for _, t in pairs]
        scores = self._model.predict(predict_pairs)

        # 3) max-pooling:每文档取 chunk 最高分
        doc_scores: dict[int, float] = {}
        for (doc_idx, _), s in zip(pairs, scores):
            s = float(s)
            if doc_idx not in doc_scores or s > doc_scores[doc_idx]:
                doc_scores[doc_idx] = s

        # 4) sigmoid 归一化 + 赋值
        raw = [doc_scores.get(i, 0.0) for i in range(len(results))]
        normed = sigmoid_normalize(raw)
        for r, s in zip(results, normed):
            r.rerank_score = s

        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return ranked[:top_k]


class SiliconFlowReranker(Reranker):
    """SiliconFlow (硅基流动) 云端 rerank API。

    使用与本地 BGE 相同的模型权重,无需 GPU。
    API 每次最多 25 个文档,超出自动分批调用。
    API 返回的 relevance_score 已在 0-1 范围,无需 sigmoid。
    """

    name = "siliconflow"
    _MAX_DOCS_PER_CALL = 25

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "BAAI/bge-reranker-v2-m3",
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ):
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/rerank"
        self._model = model
        self.name = f"siliconflow:{model.split('/')[-1]}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        if not results:
            return []

        # 1) 构建 (result_idx, chunk_text) pairs
        pairs: List[tuple[int, str]] = []
        for i, r in enumerate(results):
            chunks = chunk_text(
                r.text_for_rerank(), self._chunk_max_chars, self._chunk_overlap
            )
            for c in chunks:
                pairs.append((i, c))

        if not pairs:
            return results[:top_k]

        # 2) 分批调用 API (每次最多 25 个文档)
        doc_scores: dict[int, float] = {}
        documents = [t for _, t in pairs]

        for batch_start in range(0, len(documents), self._MAX_DOCS_PER_CALL):
            batch = documents[batch_start : batch_start + self._MAX_DOCS_PER_CALL]
            resp = _requests.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "query": query,
                    "documents": batch,
                    "top_n": len(batch),
                    "return_documents": False,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", []):
                global_idx = batch_start + item["index"]
                doc_idx = pairs[global_idx][0]
                s = float(item["relevance_score"])
                if doc_idx not in doc_scores or s > doc_scores[doc_idx]:
                    doc_scores[doc_idx] = s

        # 3) 赋值 + 排序 (API 分数已在 0-1 范围,无需 sigmoid)
        for i, r in enumerate(results):
            r.rerank_score = doc_scores.get(i, 0.0)

        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return ranked[:top_k]


class ThresholdReranker(Reranker):
    """包装器:在内部 reranker 基础上增加 sigmoid 阈值过滤。"""

    def __init__(self, inner: Reranker, threshold: float = 0.3):
        self._inner = inner
        self._threshold = threshold
        self.name = inner.name

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        ranked = self._inner.rerank(query, results, top_k)
        if self._threshold <= 0:
            return ranked
        return [r for r in ranked if (r.rerank_score or 0) >= self._threshold][:top_k]


# --- 日期解析 ---

_DATE_PATTERNS = [
    _re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),   # 2026-06-05
    _re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})"),   # 2026.06.05
    _re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"),     # 2026/06/05
    _re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),  # 2026年6月5日
]


def _parse_date_days_ago(date_str: str) -> Optional[int]:
    """解析日期字符串,返回距今天数。失败返回 None。"""
    if not date_str:
        return None
    for pat in _DATE_PATTERNS:
        m = pat.search(date_str)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                return max(0, (datetime.now(timezone.utc) - dt).days)
            except (ValueError, TypeError):
                continue
    return None


# --- 来源权威度默认权重 ---

_DEFAULT_AUTHORITY = {
    "serpapi": 0.95,   # Google 索引
    "tencent": 0.90,   # 腾讯自有索引
    "baidu": 0.85,     # 百度自有索引
}


class FusionReranker(Reranker):
    """辅助信号融合:在文本相关性基础上融合新鲜度、来源权威度、源内排名。

    final_score = α·text + β·freshness + γ·authority + δ·rank_signal

    默认权重: α=0.7, β=0.15, γ=0.1, δ=0.05
    """

    def __init__(
        self,
        inner: Reranker,
        time_sensitive: bool = False,
        alpha: float = 0.7,
        beta: float = 0.15,
        gamma: float = 0.10,
        delta: float = 0.05,
        authority_weights: Optional[dict] = None,
    ):
        self._inner = inner
        self._time_sensitive = time_sensitive
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._delta = delta
        self._authority = authority_weights or _DEFAULT_AUTHORITY
        self.name = inner.name

    def _freshness_score(self, days_ago: Optional[int]) -> float:
        """新鲜度分数(0-1)。时效查询用指数衰减,非时效查询用线性衰减。"""
        if days_ago is None:
            return 0.5  # 无日期信息,给中性分
        if self._time_sensitive:
            return 1.0 / (1.0 + days_ago)  # 时效:1天=0.5, 7天=0.125
        return max(0.0, 1.0 - days_ago / 365.0)  # 非时效:线性衰减到0(1年)

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        ranked = self._inner.rerank(query, results, top_k)
        if not ranked:
            return ranked

        for r in ranked:
            text = r.rerank_score or 0.0
            freshness = self._freshness_score(_parse_date_days_ago(r.date))
            authority = self._authority.get(r.source, 0.8)
            rank_signal = 1.0 / (1.0 + (r.provider_rank or 0))

            r.rerank_score = (
                self._alpha * text
                + self._beta * freshness
                + self._gamma * authority
                + self._delta * rank_signal
            )

        return sorted(ranked, key=lambda r: r.rerank_score or 0.0, reverse=True)[:top_k]


def build_reranker(
    enabled: bool,
    backend: str,
    model_name: str,
    cache_dir: str,
    device: Optional[str] = None,
    chunk_max_chars: int = 400,
    chunk_overlap: int = 50,
    threshold: float = 0.3,
    siliconflow_api_key: str = "",
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1",
    fusion_enabled: bool = True,
    fusion_time_sensitive: bool = False,
    fusion_alpha: float = 0.7,
    fusion_beta: float = 0.15,
    fusion_gamma: float = 0.10,
    fusion_delta: float = 0.05,
) -> Reranker:
    """构建重排器:backend → ThresholdReranker → FusionReranker,失败回退 NoOp。"""
    if not enabled or backend == "none":
        return NoOpReranker()
    try:
        if backend == "bge":
            inner: Reranker = BGEReranker(
                model_name, device=device,
                chunk_max_chars=chunk_max_chars, chunk_overlap=chunk_overlap,
            )
        elif backend == "flashrank":
            inner = FlashRankReranker(
                model_name, cache_dir,
                chunk_max_chars=chunk_max_chars, chunk_overlap=chunk_overlap,
            )
        elif backend == "siliconflow":
            if not siliconflow_api_key:
                print("[rerank] siliconflow: 未配置 SILICONFLOW_API_KEY,回退 NoOp")
                return NoOpReranker()
            inner = SiliconFlowReranker(
                api_key=siliconflow_api_key, base_url=siliconflow_base_url,
                model=model_name,
                chunk_max_chars=chunk_max_chars, chunk_overlap=chunk_overlap,
            )
        else:
            return NoOpReranker()
        r: Reranker = ThresholdReranker(inner, threshold=threshold)
        if fusion_enabled:
            r = FusionReranker(
                r,
                time_sensitive=fusion_time_sensitive,
                alpha=fusion_alpha, beta=fusion_beta,
                gamma=fusion_gamma, delta=fusion_delta,
            )
        return r
    except Exception as e:
        print(f"[rerank] backend={backend} 不可用,回退 NoOp: {e}")
        return NoOpReranker()
