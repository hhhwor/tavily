"""搜索引擎编排:多源并发检索 → 去重 → 重排 → Top-K。

web 搜索(腾讯/百度/SerpAPI)与学术检索(OpenAlex)并发召回、独立重排,
学术结果单独成块返回(academic_results),互不污染。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from src.config import settings
from src.cache import build_cache
from src.l0 import plan_query, rewrite_academic_query
from src.models import AcademicResult, PatentResult, SearchResponse, SearchResult
from src.pipeline.dedup import dedup
from src.pipeline.fusion import rrf_fuse
from src.pipeline.rerank import AcademicReranker, NoOpReranker, FusionReranker, build_reranker
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
            elif name == "serpapi":
                from src.providers.serpapi import SerpApiProvider

                providers.append(SerpApiProvider(timeout=settings.provider_timeout))
        except Exception as e:  # 凭证缺失等
            print(f"[engine] 跳过 provider {name}: {e}")
    return providers


def _build_academic_provider() -> Optional[SearchProvider]:
    """学术检索源(OpenAlex);未启用或构建失败返回 None。"""
    if not settings.academic_enabled:
        return None
    try:
        from src.providers.openalex import OpenAlexProvider

        return OpenAlexProvider(
            base_url=settings.openalex_api_url,
            api_key=settings.openalex_api_key,
            per_page=settings.openalex_per_page,
            timeout=settings.provider_timeout,
        )
    except Exception as e:
        print(f"[engine] 跳过学术源 openalex_local: {e}")
        return None


def _build_patent_provider() -> Optional[SearchProvider]:
    """专利检索源(houdutech 只读 ES);未启用或构建失败返回 None。"""
    if not settings.patent_enabled:
        return None
    try:
        from src.providers.patent_es import PatentEsProvider

        return PatentEsProvider(
            base_url=settings.patent_es_url,
            index=settings.patent_es_index,
            timeout=settings.provider_timeout,
            verify_tls=settings.patent_es_verify_tls,
            per_page=settings.patent_es_per_page,
        )
    except Exception as e:
        print(f"[engine] 跳过专利源 patent_es: {e}")
        return None


class SearchEngine:
    def __init__(self) -> None:
        self.providers = _build_providers()
        self.academic_provider = _build_academic_provider()
        self.patent_provider = _build_patent_provider()
        self._reranker_cache: dict = {}  # 按请求参数缓存重排器(避免本地模型重复加载)
        self.cache = build_cache(settings.cache_backend, settings.cache_max_size) \
            if settings.cache_enabled else None  # provider 召回结果缓存
        self.reranker = self._make_reranker(
            settings.rerank_enabled, settings.rerank_backend,
            settings.rerank_model, settings.rerank_threshold, settings.fusion_enabled,
        )
        if not self.providers and not self.academic_provider and not self.patent_provider:
            print("[engine] 警告:无可用搜索源,请检查 .env 凭证")

    def _cached_search(
        self, prov: SearchProvider, query: str, k: int,
        recency: Optional[str], use_cache: bool,
    ) -> List[SearchResult]:
        """带缓存的 provider 召回。命中/写入均用深拷贝,避免缓存对象被后续重排原地修改污染。

        key 由 (provider, k, recency, query) 构成 —— provider 自身配置(如 openalex
        topic_filter)进程内不变,故不入 key。
        """
        if not use_cache or self.cache is None:
            return prov.search(query, k, recency)
        ck = f"{prov.name}|{k}|{recency or ''}|{query}"
        hit = self.cache.get(ck)
        if hit is not None:
            return [r.model_copy(deep=True) for r in hit]  # 返回副本,后续修改不污染缓存
        items = prov.search(query, k, recency)
        self.cache.set(ck, [r.model_copy(deep=True) for r in items], settings.cache_ttl)
        return items

    def _make_reranker(self, enabled: bool, backend: str, model: str,
                       threshold: float, fusion: bool):
        """按给定参数构建重排器(其余参数取全局 settings)。"""
        return build_reranker(
            enabled, backend, model, settings.rerank_cache_dir, settings.rerank_device,
            chunk_max_chars=settings.chunk_max_chars,
            chunk_overlap=settings.chunk_overlap,
            threshold=threshold,
            siliconflow_api_key=settings.siliconflow_api_key,
            siliconflow_base_url=settings.siliconflow_base_url,
            fusion_enabled=fusion,
            fusion_alpha=settings.fusion_alpha,
            fusion_beta=settings.fusion_beta,
            fusion_gamma=settings.fusion_gamma,
            fusion_delta=settings.fusion_delta,
        )

    def _select_reranker(
        self,
        enabled: Optional[bool] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        threshold: Optional[float] = None,
        fusion: Optional[bool] = None,
    ):
        """选择重排器:全部覆盖为 None 时复用默认单例(零开销);否则按覆盖参数
        构建并缓存。缓存上限 16,超出清空,防本地模型缓存无限增长。"""
        if enabled is None and backend is None and model is None \
                and threshold is None and fusion is None:
            return self.reranker
        eff = (
            settings.rerank_enabled if enabled is None else enabled,
            backend or settings.rerank_backend,
            model or settings.rerank_model,
            settings.rerank_threshold if threshold is None else threshold,
            settings.fusion_enabled if fusion is None else fusion,
        )
        r = self._reranker_cache.get(eff)
        if r is None:
            if len(self._reranker_cache) >= 16:
                self._reranker_cache.clear()
            r = self._make_reranker(*eff)
            self._reranker_cache[eff] = r
        return r

    def search(
        self, query: str, top_k: int = 0, include_academic: Optional[bool] = None,
        include_patent: Optional[bool] = None,
        *,
        rerank_enabled: Optional[bool] = None,
        rerank_backend: Optional[str] = None,
        rerank_model: Optional[str] = None,
        rerank_threshold: Optional[float] = None,
        fusion_enabled: Optional[bool] = None,
        rewrite_enabled: Optional[bool] = None,
    ) -> SearchResponse:
        top_k = top_k or settings.default_top_k
        t0 = time.time()

        # 按请求参数选择重排器(全 None 时复用默认单例)
        reranker = self._select_reranker(
            rerank_enabled, rerank_backend, rerank_model, rerank_threshold, fusion_enabled
        )
        # 学术不复用 web 的辅助信号融合(来源权威度/源内排名是 web 假设);仅复用文本重排器。
        academic_text_reranker = self._select_reranker(
            rerank_enabled, rerank_backend, rerank_model, rerank_threshold, False
        )
        academic_reranker = AcademicReranker(academic_text_reranker)
        # 查询改写开关:请求未指定则用全局默认
        rewrite = settings.rewrite_enabled if rewrite_enabled is None else rewrite_enabled

        # 0) L0 查询理解:规范化 + 时效识别 + 学术意图识别 + (可选)LLM 改写
        plan = plan_query(
            query, [p.name for p in self.providers], top_k,
            rewrite=rewrite,
            rewrite_api_key=settings.siliconflow_api_key,
            rewrite_base_url=settings.siliconflow_base_url,
            rewrite_model=settings.rewrite_model,
            rewrite_cache_size=settings.rewrite_cache_size,
            academic_detect=settings.openalex_academic_detect,
            force_academic=include_academic,
            patent_detect=settings.patent_detect,
            force_patent=include_patent,
        )
        active = [p for p in self.providers if p.name in plan.providers]
        do_academic = self.academic_provider is not None and plan.academic
        do_patent = self.patent_provider is not None and plan.patent
        # 用改写后的查询检索(若有),否则用规范化查询
        search_query = plan.rewritten_query or plan.normalized_query
        # 学术检索单独改写 query:把自然语言问句提取为论文标题/英文检索词(web 仍用原 query)
        academic_query = search_query
        if do_academic and settings.openalex_query_rewrite and settings.siliconflow_api_key:
            academic_query = rewrite_academic_query(
                search_query, settings.siliconflow_api_key,
                settings.siliconflow_base_url, settings.rewrite_model,
                settings.rewrite_cache_size,
            )
        # 更新融合重排器的时效标记
        if isinstance(reranker, FusionReranker):
            reranker._time_sensitive = plan.time_sensitive
        elif hasattr(reranker, '_inner') and isinstance(reranker._inner, FusionReranker):
            reranker._inner._time_sensitive = plan.time_sensitive

        # 1) 并发召回:web 源 + (可选)学术源,同一个线程池
        #    缓存:provider 召回级;时效查询(time_sensitive)跳过缓存以保证新鲜度
        #    各 task 携带自己的 query(web 用原 query,学术用改写后 query)
        raw: List[SearchResult] = []
        papers: List[AcademicResult] = []
        patents: List[PatentResult] = []
        used: List[str] = []
        use_cache = settings.cache_enabled and self.cache is not None and not plan.time_sensitive
        # task: (kind, provider, query)
        tasks = [("web", p, search_query) for p in active]
        if do_academic:
            tasks.append(("academic", self.academic_provider, academic_query))
        if do_patent:
            # 专利用中文原 query(中文库;不走学术英文改写)
            tasks.append(("patent", self.patent_provider, search_query))

        if tasks:
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                futures = {
                    pool.submit(
                        self._cached_search, prov, q,
                        settings.per_provider_k, plan.recency, use_cache,
                    ): (kind, prov.name)
                    for kind, prov, q in tasks
                }
                for fut in as_completed(futures):
                    kind, name = futures[fut]
                    try:
                        items = fut.result()
                        if kind == "academic":
                            papers.extend(items)
                            if items:
                                used.append(name)  # 学术源也计入来源归属(供 providers_used)
                        elif kind == "patent":
                            patents.extend(items)
                            if items:
                                used.append(name)  # 专利源也计入来源归属
                        else:
                            for i, r in enumerate(items):
                                r.provider_rank = i  # 记录源内排名,供 RRF 融合
                            raw.extend(items)
                            used.append(name)
                    except Exception as e:
                        print(f"[engine] provider {name} 失败: {e}")

        # 2) 多路独立重排(并发),复用选定的 reranker
        #    web: NoOp 走 RRF 融合;否则 dedup 后 cross-encoder 重排
        #    学术/专利: 单源,直接 reranker 打分(NoOp 时按来源原始分排序)
        def _rank_web() -> List[SearchResult]:
            if isinstance(reranker, NoOpReranker):
                return rrf_fuse(raw, top_k=top_k)
            return reranker.rerank(search_query, dedup(raw), top_k)

        def _rank_academic() -> List[AcademicResult]:
            if not papers:
                return []
            # 用改写后的学术检索词重排(英文↔英文论文打分更准,避免中文原query被阈值误杀)
            ranked = academic_reranker.rerank(academic_query, papers, top_k)
            return [r for r in ranked if isinstance(r, AcademicResult)]

        def _rank_patent() -> List[PatentResult]:
            if not patents:
                return []
            # 专利用中文原 query 重排(中文库;NoOp 时 reranker 内部按 score=ES _score 排序兜底)
            ranked = reranker.rerank(search_query, patents, top_k)
            return [r for r in ranked if isinstance(r, PatentResult)]

        # 多于一路有结果时并发重排;否则顺序执行(零线程开销)
        rank_jobs = [_rank_web]
        if papers:
            rank_jobs.append(_rank_academic)
        if patents:
            rank_jobs.append(_rank_patent)
        if len(rank_jobs) > 1:
            with ThreadPoolExecutor(max_workers=len(rank_jobs)) as pool:
                fut_web = pool.submit(_rank_web)
                fut_acad = pool.submit(_rank_academic)
                fut_pat = pool.submit(_rank_patent)
                ranked = fut_web.result()
                ranked_papers = fut_acad.result()
                ranked_patents = fut_pat.result()
        else:
            ranked = _rank_web()
            ranked_papers = _rank_academic()
            ranked_patents = _rank_patent()

        return SearchResponse(
            query=query,
            normalized_query=plan.normalized_query,
            rewritten_query=plan.rewritten_query,
            recency=plan.recency,
            time_sensitive=plan.time_sensitive,
            results=ranked,
            academic_results=ranked_papers,
            academic_query=academic_query if do_academic else None,
            patent_results=ranked_patents,
            patent_query=search_query if do_patent else None,
            count=len(ranked),
            providers_used=used,
            reranker=reranker.name,
            elapsed_ms=int((time.time() - t0) * 1000),
        )


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "2026年人工智能最新进展"
    resp = SearchEngine().search(q)
    print(
        f"\n query={resp.query!r}  norm={resp.normalized_query!r}"
        + (f"  rewrite={resp.rewritten_query!r}" if resp.rewritten_query else "")
        + f"\n recency={resp.recency} time_sensitive={resp.time_sensitive}\n"
        f" sources={resp.providers_used}  reranker={resp.reranker}  "
        f"{resp.count} 条 web"
        + (f" + {len(resp.academic_results)} 条论文" if resp.academic_results else "")
        + (f" + {len(resp.patent_results)} 条专利" if resp.patent_results else "")
        + f"  {resp.elapsed_ms}ms\n"
    )
    for i, r in enumerate(resp.results, 1):
        rs = f" rerank={r.rerank_score:.3f}" if r.rerank_score is not None else ""
        print(f"[{i}] {r.title}  ({r.source} | {r.site} {r.date}){rs}")
        print(f"    {r.url}")
        print(f"    {(r.snippet or r.content)[:110]}\n")

    if resp.academic_results:
        print(" ── 学术论文 ──")
        for i, p in enumerate(resp.academic_results, 1):
            rs = f" rerank={p.rerank_score:.3f}" if p.rerank_score is not None else ""
            authors = ", ".join(p.authors[:3]) + ("等" if len(p.authors) > 3 else "")
            print(f"[{i}] {p.title}  ({p.year} | {p.venue} | 被引{p.citations}){rs}")
            print(f"    {authors}")
            print(f"    {p.url}")
            print(f"    {(p.snippet or p.content)[:110]}\n")

    if resp.patent_results:
        print(" ── 专利 ──")
        for i, p in enumerate(resp.patent_results, 1):
            rs = f" rerank={p.rerank_score:.3f}" if p.rerank_score is not None else ""
            applicant = ", ".join(p.applicant[:2]) + ("等" if len(p.applicant) > 2 else "")
            cls = p.ipc_main or p.cpc_main or "-"
            print(f"[{i}] {p.title}  ({p.country} {p.patent_type} | {p.publication_number} | 分类 {cls}){rs}")
            print(f"    申请人: {applicant}  申请日: {p.application_date}")
            print(f"    {p.url}")
            print(f"    {(p.snippet or p.content)[:110]}\n")
