"""RRF (Reciprocal Rank Fusion) 多源融合。

替代"按来源原始分排序"——后者在不同来源分数语义不一致时会失真
(例:腾讯有 score、百度没有,导致百度结果被无条件压到后面)。

RRF 只看各来源内的排名,与绝对分数无关:
    score(doc) = Σ_source 1 / (k + rank_in_source)
同一文档在多个来源出现会累加,天然实现"多源共识加权"。k 默认 60(标准取值)。

依赖每条结果的 provider_rank(在所属来源内的 0-based 排名)。
"""
from __future__ import annotations

from typing import List, Optional

from src.models import SearchResult
from src.pipeline.dedup import normalize_url


def rrf_prepare(
    results: List[SearchResult], k_rrf: int = 60
) -> List[SearchResult]:
    """Build stable RRF candidates and retain the prior as pipeline metadata."""
    candidates = [result.model_copy(deep=True) for result in results]
    groups: dict[str, list] = {}
    order: List[str] = []
    for index, result in enumerate(candidates):
        key = normalize_url(result.url) or result.url or f"web:{index}"
        rank = result.provider_rank if result.provider_rank is not None else 0
        contrib = 1.0 / (k_rrf + rank + 1)  # rank 0-based,故 +1
        if key in groups:
            representative, score, first_index = groups[key]
            # 保留正文更全的代表,合并来源标记
            keep, other = (
                (representative, result)
                if len(representative.content) >= len(result.content)
                else (result, representative)
            )
            if other.source and other.source not in keep.source:
                keep.source = f"{keep.source}+{other.source}" if keep.source else other.source
            groups[key] = [keep, score + contrib, first_index]
        else:
            groups[key] = [result, contrib, index]
            order.append(key)

    prepared: List[SearchResult] = []
    for key in order:
        representative, score, first_index = groups[key]
        representative.raw["_rrf_prior"] = score
        representative.raw["_rrf_first_idx"] = first_index
        prepared.append(representative)
    prepared.sort(
        key=lambda result: (
            -float(result.raw.get("_rrf_prior", 0.0)),
            result.provider_rank if result.provider_rank is not None else 10**9,
            result.url,
        )
    )
    return prepared


def rrf_fuse(
    results: List[SearchResult], k_rrf: int = 60, top_k: Optional[int] = None
) -> List[SearchResult]:
    """对带 provider_rank 的多源结果做 RRF 融合 + 去重。"""
    fused = rrf_prepare(results, k_rrf=k_rrf)
    for result in fused:
        result.rerank_score = float(result.raw["_rrf_prior"])
    return fused[:top_k] if top_k else fused
