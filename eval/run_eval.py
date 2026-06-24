"""IR 评测编排:多配置对照 + LLM-judge 分级相关性 + IR 指标。

用法(项目根目录):
  .venv/bin/python -m eval.run_eval                 # 全量
  .venv/bin/python -m eval.run_eval --max-queries 4 # 快速冒烟
  .venv/bin/python -m eval.run_eval --no-judge      # 只看延迟,不调 judge

对照配置:
  tencent+noop / baidu+noop / dual+noop / dual+flashrank
回答:哪个源强?多源值不值?重排值不值(质量↑ vs 延迟↑)?
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Dict, List, Tuple

from src.config import settings
from src.l0 import rewrite_academic_query
from src.models import SearchResult, PatentResult, AcademicResult
from src.pipeline.dedup import dedup
from src.pipeline.fusion import rrf_fuse
from src.pipeline.rerank import (
    BGEReranker, SiliconFlowReranker, FusionReranker, ThresholdReranker, NoOpReranker,
)

from eval import metrics as M

# (名称, 来源列表, 策略)
# 策略: orig=原始去重 / rrf=RRF融合 / bge=BGE重排 / sf=SiliconFlow重排 / sf+fusion=SF重排+信号融合
CONFIGS: List[Tuple[str, List[str], str]] = [
    ("tencent", ["tencent"], "orig"),
    ("baidu", ["baidu"], "orig"),
    ("serpapi", ["serpapi"], "orig"),
    ("triple+rrf", ["tencent", "baidu", "serpapi"], "rrf"),
    ("triple+sf", ["tencent", "baidu", "serpapi"], "sf"),
    ("triple+sf+fusion", ["tencent", "baidu", "serpapi"], "sf+fusion"),
]
# 专利支线评测配置(单源 = houdutech 只读 ES);判分用专利 rubric、独立池。
#   noop   = 按 ES _score 原序(不重排)
#   sf     = SiliconFlow 交叉编码器重排(无阈值)
#   sf+thr = SF 重排 + 阈值过滤,复测 RERANK_THRESHOLD 是否误杀相关专利(§8 TODO ④)
PATENT_CONFIGS: List[Tuple[str, str]] = [
    ("patent+noop", "noop"),
    ("patent+sf", "sf"),
    ("patent+sf+thr", "sf+thr"),
]
# 学术支线评测配置(单源 = OpenAlex 经 Chukonu ES);判分用学术 rubric、独立池。
# 与专利支线同构:复测重排是否有增益、阈值是否误杀相关论文。
#   noop   = 按 ES _score 原序(不重排)
#   sf     = SiliconFlow 交叉编码器重排(无阈值)
#   sf+thr = SF 重排 + 阈值过滤
# 注意:召回与重排均用「学术改写后查询」(英文检索词),与引擎学术路径一致。
ACADEMIC_CONFIGS: List[Tuple[str, str]] = [
    ("academic+noop", "noop"),
    ("academic+sf", "sf"),
    ("academic+sf+thr", "sf+thr"),
]
_PROVIDERS = ["tencent", "baidu", "serpapi"]
_CACHE_DIR = "eval/cache"


def load_queries(path: str, limit: int) -> List[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


def _provider(name: str):
    if name == "tencent":
        from src.providers.tencent import TencentSearchProvider
        return TencentSearchProvider(timeout=settings.provider_timeout)
    if name == "serpapi":
        from src.providers.serpapi import SerpApiProvider
        return SerpApiProvider(timeout=settings.provider_timeout)
    from src.providers.baidu import BaiduSearchProvider
    return BaiduSearchProvider(timeout=settings.provider_timeout)


def retrieve_cached(provider_names: List[str], queries: List[dict], k: int) -> Dict[str, dict]:
    """每个 (provider, query) 检索一次并落盘缓存,返回 {provider: {query: {latency_ms, results}}}。"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    out: Dict[str, dict] = {}
    for pname in provider_names:
        cache_file = os.path.join(_CACHE_DIR, f"search_{pname}.json")
        cache = json.load(open(cache_file, encoding="utf-8")) if os.path.exists(cache_file) else {}
        prov = None
        for row in queries:
            q = row["query"]
            if q in cache:
                continue
            if prov is None:
                prov = _provider(pname)
            t0 = time.time()
            try:
                res = prov.search(q, k)
                cache[q] = {
                    "latency_ms": int((time.time() - t0) * 1000),
                    "results": [r.model_dump() for r in res],
                }
                print(f"  [{pname}] {q[:24]}... {cache[q]['latency_ms']}ms {len(res)}条")
            except Exception as e:
                print(f"  [{pname}] {q[:24]}... 失败: {e}")
                cache[q] = {"latency_ms": -1, "results": []}
        json.dump(cache, open(cache_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        out[pname] = cache
    return out


def _results(cache: Dict[str, dict], pname: str, query: str) -> List[SearchResult]:
    data = cache.get(pname, {}).get(query, {}).get("results", [])
    return [SearchResult(**d) for d in data]


def retrieve_patents_cached(queries: List[dict], k: int) -> Dict[str, dict]:
    """专利 ES 召回(单源),落盘缓存 search_patent_es.json,返回 {query: {latency_ms, results}}。"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_CACHE_DIR, "search_patent_es.json")
    cache = json.load(open(cache_file, encoding="utf-8")) if os.path.exists(cache_file) else {}
    prov = None
    for row in queries:
        q = row["query"]
        if q in cache:
            continue
        if prov is None:
            from src.providers.patent_es import PatentEsProvider
            prov = PatentEsProvider(
                base_url=settings.patent_es_url, index=settings.patent_es_index,
                timeout=settings.provider_timeout, verify_tls=settings.patent_es_verify_tls,
                per_page=settings.patent_es_per_page,
            )
        t0 = time.time()
        try:
            res = prov.search(q, k)
            cache[q] = {
                "latency_ms": int((time.time() - t0) * 1000),
                "results": [r.model_dump() for r in res],
            }
            print(f"  [patent_es] {q[:24]}... {cache[q]['latency_ms']}ms {len(res)}条")
        except Exception as e:
            print(f"  [patent_es] {q[:24]}... 失败: {e}")
            cache[q] = {"latency_ms": -1, "results": []}
    json.dump(cache, open(cache_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return cache


def _patents(cache: Dict[str, dict], query: str) -> List[PatentResult]:
    data = cache.get(query, {}).get("results", [])
    return [PatentResult(**d) for d in data]


def run_patent_track(
    patent_queries: List[dict], k: int, sf, judge_model: str, no_judge: bool,
) -> List[str]:
    """专利支线评测:ES 召回 → 各重排策略 → 专利 rubric 判分 → IR 指标。返回报告行。"""
    print("\n[专利支线] ES 召回...")
    cache = retrieve_patents_cached(patent_queries, k)

    ranked_by: Dict[str, Dict[str, List[PatentResult]]] = {c[0]: {} for c in PATENT_CONFIGS}
    rerank_ms: Dict[str, List[float]] = {c[0]: [] for c in PATENT_CONFIGS}
    pool: Dict[str, Dict[str, str]] = {}
    print("[专利支线] 组装配置 + 收集判分池...")
    for row in patent_queries:
        q = row["query"]
        pool.setdefault(q, {})
        for name, strat in PATENT_CONFIGS:
            cands = _patents(cache, q)  # 每配置取新对象,避免重排原地改分跨配置污染
            t0 = time.time()
            if strat == "noop":
                ranked = NoOpReranker().rerank(q, cands, k)  # ES _score 原序
            elif strat == "sf":
                ranked = sf.rerank(q, cands, k)
            else:  # sf+thr:SF 重排 + 阈值过滤(复测是否误杀)
                ranked = ThresholdReranker(sf, settings.rerank_threshold).rerank(q, cands, k)
            rerank_ms[name].append((time.time() - t0) * 1000)
            ranked_by[name][q] = ranked
            for r in ranked:
                pool[q].setdefault(r.url, (r.content or r.snippet or r.title))

    judged: Dict[Tuple[str, str], int] = {}
    if not no_judge:
        print("[专利支线] LLM-judge 打分(专利 rubric)...")
        from eval.judge import ClaudeJudge, _PATENT_RUBRIC
        judge = ClaudeJudge(
            model=judge_model,
            cache_path=os.path.join(_CACHE_DIR, "judgments_patent.json"),
            rubric=_PATENT_RUBRIC,
        )
        items = [(q, url, text) for q, docs in pool.items() for url, text in docs.items()]
        print(f"  待判 {len(items)} 个 (query,专利),命中缓存的跳过...")
        judged = judge.score_batch(items)

    lat = _avg([v["latency_ms"] for v in cache.values() if v["latency_ms"] >= 0])
    lines = ["", "## 专利支线 (houdutech ES, 单源)",
             f"- 召回延迟(平均): {lat:.0f} ms  ·  查询数: {len(patent_queries)}", ""]
    if no_judge:
        lines.append("| 配置 | rerank_ms |")
        lines.append("|------|-----------|")
        for name, _ in PATENT_CONFIGS:
            lines.append(f"| {name} | {_avg(rerank_ms[name]):.0f} |")
        return lines

    lines.append("| 配置 | NDCG@k | Recall@k | P@k | MRR | rerank_ms |")
    lines.append("|------|--------|----------|-----|-----|-----------|")
    for name, _ in PATENT_CONFIGS:
        per_q = []
        for row in patent_queries:
            q = row["query"]
            pool_rels = [judged.get((q, u), 0) for u in pool[q]]
            ranked_rels = [judged.get((q, r.url), 0) for r in ranked_by[name][q]]
            per_q.append({
                "NDCG@k": M.ndcg_at_k(ranked_rels, pool_rels, k),
                "Recall@k": M.recall_at_k(ranked_rels, pool_rels, k),
                "P@k": M.precision_at_k(ranked_rels, k),
                "MRR": M.mrr(ranked_rels),
            })
        agg = M.aggregate(per_q)
        lines.append(
            f"| {name} | {agg['NDCG@k']:.3f} | {agg['Recall@k']:.3f} | "
            f"{agg['P@k']:.3f} | {agg['MRR']:.3f} | {_avg(rerank_ms[name]):.0f} |"
        )
    return lines


def retrieve_academics_cached(queries: List[dict], k: int) -> Dict[str, dict]:
    """学术 OpenAlex 召回(单源)。召回用「学术改写后查询」(与引擎学术路径一致),
    落盘缓存 search_academic.json:{query: {latency_ms, academic_query, results}}。
    改写结果随缓存冻结,保证复现(改写依赖 LLM,首跑后不再变)。"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_CACHE_DIR, "search_academic.json")
    cache = json.load(open(cache_file, encoding="utf-8")) if os.path.exists(cache_file) else {}
    prov = None
    for row in queries:
        q = row["query"]
        if q in cache:
            continue
        if prov is None:
            from src.providers.openalex import OpenAlexProvider
            prov = OpenAlexProvider(
                base_url=settings.openalex_api_url, api_key=settings.openalex_api_key,
                per_page=settings.openalex_per_page, timeout=settings.provider_timeout,
            )
        # 学术改写:把中文/口语化 query 提取为英文检索词(与 engine 一致);无 key 则用原 query
        aq = q
        if settings.openalex_query_rewrite and settings.siliconflow_api_key:
            aq = rewrite_academic_query(
                q, settings.siliconflow_api_key, settings.siliconflow_base_url,
                settings.rewrite_model, settings.rewrite_cache_size,
            )
        t0 = time.time()
        try:
            res = prov.search(aq, k)
            cache[q] = {
                "latency_ms": int((time.time() - t0) * 1000),
                "academic_query": aq,
                "results": [r.model_dump() for r in res],
            }
            print(f"  [openalex] {q[:18]}... → {aq[:26]!r} {cache[q]['latency_ms']}ms {len(res)}条")
        except Exception as e:
            print(f"  [openalex] {q[:18]}... 失败: {e}")
            cache[q] = {"latency_ms": -1, "academic_query": aq, "results": []}
    json.dump(cache, open(cache_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return cache


def _academics(cache: Dict[str, dict], query: str) -> List[AcademicResult]:
    data = cache.get(query, {}).get("results", [])
    return [AcademicResult(**d) for d in data]


def run_academic_track(
    academic_queries: List[dict], k: int, sf, judge_model: str, no_judge: bool,
) -> List[str]:
    """学术支线评测:OpenAlex 召回 → 各重排策略 → 学术 rubric 判分 → IR 指标。返回报告行。
    召回/重排/判分均以「学术改写后查询」(aq)为准,与引擎学术路径一致。"""
    print("\n[学术支线] OpenAlex 召回...")
    cache = retrieve_academics_cached(academic_queries, k)

    ranked_by: Dict[str, Dict[str, List[AcademicResult]]] = {c[0]: {} for c in ACADEMIC_CONFIGS}
    rerank_ms: Dict[str, List[float]] = {c[0]: [] for c in ACADEMIC_CONFIGS}
    pool: Dict[str, Dict[str, str]] = {}
    aq_of: Dict[str, str] = {}  # 原始 query -> 学术改写后 query(判分/池/指标统一用 aq 作 key)
    print("[学术支线] 组装配置 + 收集判分池...")
    for row in academic_queries:
        q = row["query"]
        aq = cache.get(q, {}).get("academic_query") or q
        aq_of[q] = aq
        pool.setdefault(aq, {})
        for name, strat in ACADEMIC_CONFIGS:
            cands = _academics(cache, q)  # 每配置取新对象,避免重排原地改分跨配置污染
            t0 = time.time()
            if strat == "noop":
                ranked = NoOpReranker().rerank(aq, cands, k)  # ES _score 原序
            elif strat == "sf":
                ranked = sf.rerank(aq, cands, k)
            else:  # sf+thr:SF 重排 + 阈值过滤(复测是否误杀)
                ranked = ThresholdReranker(sf, settings.rerank_threshold).rerank(aq, cands, k)
            rerank_ms[name].append((time.time() - t0) * 1000)
            ranked_by[name][aq] = ranked
            for r in ranked:
                # 判分文本含标题(摘要可能不点题),提升 judge 准确性
                pool[aq].setdefault(r.url, f"{r.title}\n{r.content or r.snippet}".strip())

    judged: Dict[Tuple[str, str], int] = {}
    if not no_judge:
        print("[学术支线] LLM-judge 打分(学术 rubric)...")
        from eval.judge import ClaudeJudge, _ACADEMIC_RUBRIC
        judge = ClaudeJudge(
            model=judge_model,
            cache_path=os.path.join(_CACHE_DIR, "judgments_academic.json"),
            rubric=_ACADEMIC_RUBRIC,
        )
        items = [(aq, url, text) for aq, docs in pool.items() for url, text in docs.items()]
        print(f"  待判 {len(items)} 个 (query,论文),命中缓存的跳过...")
        judged = judge.score_batch(items)

    lat = _avg([v["latency_ms"] for v in cache.values() if v["latency_ms"] >= 0])
    lines = ["", "## 学术支线 (OpenAlex via Chukonu ES, 单源)",
             f"- 召回延迟(平均): {lat:.0f} ms  ·  查询数: {len(academic_queries)}",
             "- 召回/重排/判分均用「学术改写后查询」(与引擎学术路径一致)", ""]
    if no_judge:
        lines.append("| 配置 | rerank_ms |")
        lines.append("|------|-----------|")
        for name, _ in ACADEMIC_CONFIGS:
            lines.append(f"| {name} | {_avg(rerank_ms[name]):.0f} |")
        return lines

    lines.append("| 配置 | NDCG@k | Recall@k | P@k | MRR | rerank_ms |")
    lines.append("|------|--------|----------|-----|-----|-----------|")
    for name, _ in ACADEMIC_CONFIGS:
        per_q = []
        for row in academic_queries:
            aq = aq_of[row["query"]]
            pool_rels = [judged.get((aq, u), 0) for u in pool[aq]]
            ranked_rels = [judged.get((aq, r.url), 0) for r in ranked_by[name][aq]]
            per_q.append({
                "NDCG@k": M.ndcg_at_k(ranked_rels, pool_rels, k),
                "Recall@k": M.recall_at_k(ranked_rels, pool_rels, k),
                "P@k": M.precision_at_k(ranked_rels, k),
                "MRR": M.mrr(ranked_rels),
            })
        agg = M.aggregate(per_q)
        lines.append(
            f"| {name} | {agg['NDCG@k']:.3f} | {agg['Recall@k']:.3f} | "
            f"{agg['P@k']:.3f} | {agg['MRR']:.3f} | {_avg(rerank_ms[name]):.0f} |"
        )
    return lines


def run_web_vs_academic_coverage(
    academic_queries: List[dict], k: int, sf, judge_model: str, no_judge: bool,
) -> List[str]:
    """补盲对比:同一批学术查询上,web 三源 vs academic 单源,放进**同一判分池**、用
    **同一「是否回答查询」web rubric** 判分,直接体现 web 对学术意图查询召回弱、academic 补盲。

    - web 用原始查询检索(同引擎 web 路径),多源 dedup + sf 重排取 Top-K;
    - academic 复用学术改写召回(同引擎学术路径),sf 重排取 Top-K;
    - 两路 Top-K 合成共享池,统一判分 → 指标可直接横比(同分母 pool)。
    判分缓存独立(judgments_acadcov.json),与各支线互不污染。
    """
    print("\n[补盲对比] web 三源召回(学术查询)...")
    web_cache = retrieve_cached(_PROVIDERS, academic_queries, k)
    acad_cache = retrieve_academics_cached(academic_queries, k)  # 已缓存,no-op

    web_ranked: Dict[str, List[SearchResult]] = {}
    acad_ranked: Dict[str, List[AcademicResult]] = {}
    pool: Dict[str, Dict[str, str]] = {}
    print("[补盲对比] 重排 + 收集共享判分池...")
    for row in academic_queries:
        q = row["query"]
        aq = acad_cache.get(q, {}).get("academic_query") or q
        merged: List[SearchResult] = []
        for p in _PROVIDERS:
            merged.extend(_results(web_cache, p, q))
        web_ranked[q] = sf.rerank(q, dedup(merged), k)           # web 用原始查询
        acad_ranked[q] = sf.rerank(aq, _academics(acad_cache, q), k)  # academic 用学术改写查询
        pool.setdefault(q, {})
        for r in web_ranked[q]:
            pool[q].setdefault(r.url, f"{r.title}\n{r.content or r.snippet}".strip())
        for r in acad_ranked[q]:
            pool[q].setdefault(r.url, f"{r.title}\n{r.content or r.snippet}".strip())

    judged: Dict[Tuple[str, str], int] = {}
    if not no_judge:
        print("[补盲对比] LLM-judge 打分(web rubric, 共享池)...")
        from eval.judge import ClaudeJudge  # 默认 _RUBRIC:「是否回答查询」
        judge = ClaudeJudge(
            model=judge_model,
            cache_path=os.path.join(_CACHE_DIR, "judgments_acadcov.json"),
        )
        items = [(q, url, text) for q, docs in pool.items() for url, text in docs.items()]
        print(f"  待判 {len(items)} 个 (query,doc),命中缓存的跳过...")
        judged = judge.score_batch(items)

    lines = ["", "## 学术补盲对比 (同 12 条学术查询 · web 三源 vs academic · 共享池/同 web rubric)",
             "- 同一查询、同一判分池、同一「是否回答查询」rubric;各取 sf 重排 Top-K。"
             "web 召回弱 ↔ academic 高 = academic 补了 web 的盲区", ""]
    if no_judge:
        return lines + ["(--no-judge: 跳过指标)"]
    lines.append("| 来源 | NDCG@k | Recall@k | P@k | MRR |")
    lines.append("|------|--------|----------|-----|-----|")
    for label, ranked_map in [
        ("web-only (三源+sf)", web_ranked),
        ("academic-only (openalex+sf)", acad_ranked),
    ]:
        per_q = []
        for row in academic_queries:
            q = row["query"]
            pool_rels = [judged.get((q, u), 0) for u in pool[q]]
            ranked_rels = [judged.get((q, r.url), 0) for r in ranked_map[q]]
            per_q.append({
                "NDCG@k": M.ndcg_at_k(ranked_rels, pool_rels, k),
                "Recall@k": M.recall_at_k(ranked_rels, pool_rels, k),
                "P@k": M.precision_at_k(ranked_rels, k),
                "MRR": M.mrr(ranked_rels),
            })
        agg = M.aggregate(per_q)
        lines.append(
            f"| {label} | {agg['NDCG@k']:.3f} | {agg['Recall@k']:.3f} | "
            f"{agg['P@k']:.3f} | {agg['MRR']:.3f} |"
        )
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max-queries", type=int, default=0)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    args = ap.parse_args()

    queries = load_queries("eval/dataset.jsonl", args.max_queries)
    # 拆轨:web 配置只跑通用查询(排除 patent/academic,避免污染 web 指标 + 省 web API);
    #       专利查询走专利支线(ES);学术查询走学术支线(OpenAlex)。各支线独立 rubric/池。
    web_queries = [r for r in queries if r.get("type") not in ("patent", "academic")]
    patent_queries = [r for r in queries if r.get("type") == "patent"]
    academic_queries = [r for r in queries if r.get("type") == "academic"]
    do_patent = bool(patent_queries) and settings.patent_enabled
    do_academic = bool(academic_queries) and settings.academic_enabled
    if patent_queries and not settings.patent_enabled:
        print("  ⚠ 数据集含 patent 查询但专利源未启用(缺 PATENT_ES_URL),跳过专利支线\n")
    if academic_queries and not settings.academic_enabled:
        print("  ⚠ 数据集含 academic 查询但学术源未启用(缺 OPENALEX_API_URL),跳过学术支线\n")
    print(f"== 评测 web {len(web_queries)} 条 + 专利 {len(patent_queries)} 条 "
          f"+ 学术 {len(academic_queries)} 条, k={args.k} ==\n")

    # 1) 检索(缓存,三源)
    print("[1/4] 检索...")
    cache = retrieve_cached(_PROVIDERS, web_queries, args.k)

    # 2) 重排器(惰性构建)
    bge = None
    if any(c[2] == "bge" for c in CONFIGS):
        print("\n  加载 BGE-Reranker-v2-m3(首次下载 ~2GB)...")
        bge = BGEReranker()
    sf = None
    # web 的 sf/sf+fusion 配置、或专利/学术支线(sf/sf+thr)任一需要时构建
    if any(c[2] in ("sf", "sf+fusion") for c in CONFIGS) or do_patent or do_academic:
        print("\n  初始化 SiliconFlow API 重排器...")
        sf = SiliconFlowReranker(api_key=settings.siliconflow_api_key)

    # 3) 每 query 跑各配置,记录策略延迟;收集 judge 池
    print("\n[2/4] 组装配置 + 收集判分池...")
    ranked_by: Dict[str, Dict[str, List[SearchResult]]] = {c[0]: {} for c in CONFIGS}
    rerank_ms: Dict[str, List[float]] = {c[0]: [] for c in CONFIGS}
    pool: Dict[str, Dict[str, str]] = {}  # query -> {url: text}
    for row in web_queries:
        q = row["query"]
        pool.setdefault(q, {})
        for name, provs, strat in CONFIGS:
            # 构建带 provider_rank 的多源候选(每配置取新对象,避免跨配置污染)
            merged: List[SearchResult] = []
            for p in provs:
                lst = _results(cache, p, q)
                for i, r in enumerate(lst):
                    r.provider_rank = i
                merged.extend(lst)
            t0 = time.time()
            if strat == "orig":
                ranked = dedup(merged)[: args.k]
            elif strat == "rrf":
                ranked = rrf_fuse(merged, top_k=args.k)
            elif strat == "sf":
                ranked = sf.rerank(q, dedup(merged), args.k)
            elif strat == "sf+fusion":
                # SF 重排后再做信号融合(新鲜度/权威度/源内排名)
                fusion = FusionReranker(sf, time_sensitive=False)
                ranked = fusion.rerank(q, dedup(merged), args.k)
            else:  # bge
                ranked = bge.rerank(q, dedup(merged), args.k)
            rerank_ms[name].append((time.time() - t0) * 1000)
            ranked_by[name][q] = ranked
            for r in ranked:
                pool[q].setdefault(r.url, (r.content or r.snippet or r.title))

    # 4) 判分
    judged: Dict[Tuple[str, str], int] = {}
    if not args.no_judge:
        print("\n[3/4] LLM-judge 打分(Claude)...")
        from eval.judge import ClaudeJudge
        judge = ClaudeJudge(model=args.judge_model)
        items = [(q, url, text) for q, docs in pool.items() for url, text in docs.items()]
        print(f"  待判 {len(items)} 个 (query,doc),命中缓存的跳过...")
        judged = judge.score_batch(items)
    else:
        print("\n[3/4] 跳过 judge(--no-judge)")

    # 5) 指标
    print("\n[4/4] 计算指标...\n")
    rows_out = []
    for name, _, _ in CONFIGS:
        per_q = []
        for row in web_queries:
            q = row["query"]
            pool_rels = [judged.get((q, u), 0) for u in pool[q]]
            ranked_rels = [judged.get((q, r.url), 0) for r in ranked_by[name][q]]
            if args.no_judge:
                continue
            per_q.append({
                "NDCG@k": M.ndcg_at_k(ranked_rels, pool_rels, args.k),
                "Recall@k": M.recall_at_k(ranked_rels, pool_rels, args.k),
                "P@k": M.precision_at_k(ranked_rels, args.k),
                "MRR": M.mrr(ranked_rels),
            })
        agg = M.aggregate(per_q) if per_q else {}
        agg["rerank_ms"] = sum(rerank_ms[name]) / len(rerank_ms[name])
        rows_out.append((name, agg))

    # 6) 输出表
    prov_lat = {
        p: _avg([v["latency_ms"] for v in cache[p].values() if v["latency_ms"] >= 0])
        for p in _PROVIDERS
    }
    lines = []
    lines.append(f"# IR 评测报告  (web={len(web_queries)}, 专利={len(patent_queries) if do_patent else 0}, 学术={len(academic_queries) if do_academic else 0}, k={args.k})\n")
    lines.append("## 检索延迟(单源平均)")
    for p in _PROVIDERS:
        lines.append(f"- {p}: {prov_lat[p]:.0f} ms")
    lines.append("")
    lines.append("## Web 配置对照")
    if args.no_judge:
        lines.append("| 配置 | rerank_ms |")
        lines.append("|------|-----------|")
        for name, agg in rows_out:
            lines.append(f"| {name} | {agg['rerank_ms']:.0f} |")
    else:
        lines.append("| 配置 | NDCG@k | Recall@k | P@k | MRR | rerank_ms |")
        lines.append("|------|--------|----------|-----|-----|-----------|")
        for name, agg in rows_out:
            lines.append(
                f"| {name} | {agg['NDCG@k']:.3f} | {agg['Recall@k']:.3f} | "
                f"{agg['P@k']:.3f} | {agg['MRR']:.3f} | {agg['rerank_ms']:.0f} |"
            )

    # 专利支线(独立 ES 召回 + 专利 rubric 判分)
    if do_patent:
        lines += run_patent_track(patent_queries, args.k, sf, args.judge_model, args.no_judge)

    # 学术支线(独立 OpenAlex 召回 + 学术 rubric 判分)
    if do_academic:
        lines += run_academic_track(academic_queries, args.k, sf, args.judge_model, args.no_judge)

    # 学术补盲对比(同查询 web 三源 vs academic,体现 academic 补 web 盲区)
    if do_academic and settings.enabled_providers:
        lines += run_web_vs_academic_coverage(
            academic_queries, args.k, sf, args.judge_model, args.no_judge
        )

    report = "\n".join(lines)
    print(report)
    with open("eval/report.md", "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print("\n→ 已写入 eval/report.md")


def _avg(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


if __name__ == "__main__":
    main()
