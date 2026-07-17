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


def rrf_fuse(
    results: List[SearchResult], k_rrf: int = 60, top_k: Optional[int] = None
) -> List[SearchResult]:
    """对带 provider_rank 的多源结果做 RRF 融合 + 去重。"""
    results = [result.model_copy(deep=True) for result in results]
    groups: dict[str, list] = {}   # key -> [代表结果, 累计RRF分]
    order: List[str] = []
    for r in results:
        key = normalize_url(r.url) or r.url
        rank = r.provider_rank if r.provider_rank is not None else 0
        contrib = 1.0 / (k_rrf + rank + 1)  # rank 0-based,故 +1
        if key in groups:
            rep, sc = groups[key]
            # 保留正文更全的代表,合并来源标记
            keep, other = (rep, r) if len(rep.content) >= len(r.content) else (r, rep)
            if other.source and other.source not in keep.source:
                keep.source = f"{keep.source}+{other.source}"
            groups[key] = [keep, sc + contrib]
        else:
            groups[key] = [r, contrib]
            order.append(key)

    fused = []
    for key in order:
        rep, sc = groups[key]
        rep.rerank_score = sc
        fused.append(rep)
    fused.sort(key=lambda x: x.rerank_score or 0.0, reverse=True)
    return fused[:top_k] if top_k else fused
