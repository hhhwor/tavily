"""IR 评测指标(纯标准库)。

基于分级相关性(graded relevance, 0-3)+ pooling 计算:
  - NDCG@k : 排序质量(用分级相关性,2^rel-1 增益)
  - Recall@k / Precision@k / MRR : 二值相关性(rel >= 阈值,默认 2)

ranked_rels : 某配置返回的 Top-K,按排名顺序的相关性分级列表
pool_rels   : 该 query 下所有被判过分的文档(各配置 Top-K 的并集)的相关性分级
"""
from __future__ import annotations

import math
from typing import List


def _dcg(rels: List[int]) -> float:
    return sum((2 ** rel - 1) / math.log2(i + 2) for i, rel in enumerate(rels))


def ndcg_at_k(ranked_rels: List[int], pool_rels: List[int], k: int) -> float:
    """归一化折损累计增益。IDCG 用 pool 内最优排序。"""
    dcg = _dcg(ranked_rels[:k])
    ideal = sorted(pool_rels, reverse=True)[:k]
    idcg = _dcg(ideal)
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(ranked_rels: List[int], pool_rels: List[int], k: int, thr: int = 2) -> float:
    """Top-K 命中的相关文档数 / pool 内相关文档总数。"""
    total = sum(1 for r in pool_rels if r >= thr)
    if total == 0:
        return 0.0
    hit = sum(1 for r in ranked_rels[:k] if r >= thr)
    return hit / total


def precision_at_k(ranked_rels: List[int], k: int, thr: int = 2) -> float:
    if k == 0:
        return 0.0
    return sum(1 for r in ranked_rels[:k] if r >= thr) / k


def mrr(ranked_rels: List[int], thr: int = 2) -> float:
    """首个相关文档的倒数排名。"""
    for i, r in enumerate(ranked_rels, 1):
        if r >= thr:
            return 1.0 / i
    return 0.0


def aggregate(per_query: List[dict]) -> dict:
    """对多条 query 的指标求宏平均。"""
    if not per_query:
        return {}
    keys = per_query[0].keys()
    return {k: sum(q[k] for q in per_query) / len(per_query) for k in keys}
