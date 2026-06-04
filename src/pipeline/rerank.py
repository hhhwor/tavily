"""重排序:FlashRank 轻量 cross-encoder(无 torch),失败回退 NoOp。

FlashRank 用 ONNX 小模型,首次使用会下载到 cache_dir(默认 /data)。
若不可用(未装/下载失败),自动回退到 NoOpReranker —— 保证管线始终可跑。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from src.models import SearchResult


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


class FlashRankReranker(Reranker):
    """FlashRank cross-encoder 重排。"""

    name = "flashrank"

    def __init__(self, model_name: str, cache_dir: str):
        from flashrank import Ranker  # 延迟导入,避免未装时报错

        self._ranker = Ranker(model_name=model_name, cache_dir=cache_dir)
        self.name = f"flashrank:{model_name}"

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        from flashrank import RerankRequest

        if not results:
            return []
        passages = [
            {"id": i, "text": r.text_for_rerank()[:2000]} for i, r in enumerate(results)
        ]
        ranked = self._ranker.rerank(RerankRequest(query=query, passages=passages))
        out: List[SearchResult] = []
        for item in ranked:
            r = results[item["id"]]
            r.rerank_score = float(item["score"])
            out.append(r)
        return out[:top_k]


class BGEReranker(Reranker):
    """BGE-Reranker-v2-m3 cross-encoder(多语言,中文强;需 torch)。"""

    name = "bge"

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", max_length: int = 512,
                 device: Optional[str] = None):
        from sentence_transformers import CrossEncoder  # 延迟导入

        kwargs = {"max_length": max_length}
        if device:
            kwargs["device"] = device  # None=自动(有 GPU 用 GPU)
        self._model = CrossEncoder(model_name, **kwargs)
        self.name = f"bge:{model_name.split('/')[-1]}"

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        if not results:
            return []
        pairs = [(query, r.text_for_rerank()[:2000]) for r in results]
        scores = self._model.predict(pairs)
        for r, s in zip(results, scores):
            r.rerank_score = float(s)
        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return ranked[:top_k]


def build_reranker(
    enabled: bool, backend: str, model_name: str, cache_dir: str,
    device: Optional[str] = None,
) -> Reranker:
    """构建重排器:按 backend 选择,失败回退 NoOp,保证管线始终可跑。"""
    if not enabled or backend == "none":
        return NoOpReranker()
    try:
        if backend == "bge":
            return BGEReranker(model_name, device=device)
        if backend == "flashrank":
            return FlashRankReranker(model_name, cache_dir)
        return NoOpReranker()
    except Exception as e:  # 未装 / 下载失败 / 初始化错误
        print(f"[rerank] backend={backend} 不可用,回退 NoOp: {e}")
        return NoOpReranker()
