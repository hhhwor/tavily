"""搜索引擎编排:多源并发检索 → 去重 → 重排 → Top-K。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

from src.config import settings
from src.l0 import plan_query
from src.models import SearchResponse, SearchResult
from src.pipeline.dedup import dedup
from src.pipeline.fusion import rrf_fuse
from src.pipeline.rerank import NoOpReranker, build_reranker
from src.providers.base import SearchProvider


def _build_providers() -> List[SearchProvider]:
    providers: List[SearchProvider] = []
    for name in settings.enabled_providers:
        try:
            if name == "tencent":
                from src.providers.tencent import TencentSearchProvider

                providers.append(TencentSearchProvider(timeout=settings.provider_timeout))
            elif name == "baidu":
                from src.providers.baidu import BaiduSearchProvider

                providers.append(BaiduSearchProvider(timeout=settings.provider_timeout))
        except Exception as e:  # 凭证缺失等
            print(f"[engine] 跳过 provider {name}: {e}")
    return providers


class SearchEngine:
    def __init__(self) -> None:
        self.providers = _build_providers()
        self.reranker = build_reranker(
            settings.rerank_enabled, settings.rerank_backend,
            settings.rerank_model, settings.rerank_cache_dir, settings.rerank_device,
        )
        if not self.providers:
            print("[engine] 警告:无可用搜索源,请检查 .env 凭证")

    def search(self, query: str, top_k: int = 0) -> SearchResponse:
        top_k = top_k or settings.default_top_k
        t0 = time.time()

        # 0) L0 查询理解:规范化 + 时效识别 + 路由
        plan = plan_query(query, [p.name for p in self.providers], top_k)
        active = [p for p in self.providers if p.name in plan.providers]

        # 1) 多源并发检索(用规范化查询 + 时效过滤)
        raw: List[SearchResult] = []
        used: List[str] = []
        if active:
            with ThreadPoolExecutor(max_workers=len(active)) as pool:
                futures = {
                    pool.submit(
                        p.search, plan.normalized_query, settings.per_provider_k, plan.recency
                    ): p.name
                    for p in active
                }
                for fut in as_completed(futures):
                    name = futures[fut]
                    try:
                        items = fut.result()
                        for i, r in enumerate(items):
                            r.provider_rank = i  # 记录源内排名,供 RRF 融合
                        raw.extend(items)
                        used.append(name)
                    except Exception as e:
                        print(f"[engine] provider {name} 失败: {e}")

        # 2) 去重/融合  3) 重排(用规范化查询打分)
        #   启用重排:dedup 后交 cross-encoder;
        #   未启用:用 RRF 多源融合(避免按不一致的来源分数排序而失真)
        if isinstance(self.reranker, NoOpReranker):
            ranked = rrf_fuse(raw, top_k=top_k)
        else:
            ranked = self.reranker.rerank(plan.normalized_query, dedup(raw), top_k)

        return SearchResponse(
            query=query,
            normalized_query=plan.normalized_query,
            recency=plan.recency,
            time_sensitive=plan.time_sensitive,
            results=ranked,
            count=len(ranked),
            providers_used=used,
            reranker=self.reranker.name,
            elapsed_ms=int((time.time() - t0) * 1000),
        )


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "2026年人工智能最新进展"
    resp = SearchEngine().search(q)
    print(
        f"\n query={resp.query!r}  norm={resp.normalized_query!r}  "
        f"recency={resp.recency} time_sensitive={resp.time_sensitive}\n"
        f" sources={resp.providers_used}  reranker={resp.reranker}  "
        f"{resp.count} 条  {resp.elapsed_ms}ms\n"
    )
    for i, r in enumerate(resp.results, 1):
        rs = f" rerank={r.rerank_score:.3f}" if r.rerank_score is not None else ""
        print(f"[{i}] {r.title}  ({r.source} | {r.site} {r.date}){rs}")
        print(f"    {r.url}")
        print(f"    {(r.snippet or r.content)[:110]}\n")
