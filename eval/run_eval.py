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
from src.models import SearchResult
from src.pipeline.dedup import dedup
from src.pipeline.fusion import rrf_fuse
from src.pipeline.rerank import BGEReranker

from eval import metrics as M

# (名称, 来源列表, 策略)  策略: orig=原始顺序去重 / rrf=RRF融合 / bge=BGE重排
CONFIGS: List[Tuple[str, List[str], str]] = [
    ("tencent", ["tencent"], "orig"),
    ("baidu", ["baidu"], "orig"),
    ("dual+rrf", ["tencent", "baidu"], "rrf"),
    ("dual+bge", ["tencent", "baidu"], "bge"),
]
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max-queries", type=int, default=0)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    args = ap.parse_args()

    queries = load_queries("eval/dataset.jsonl", args.max_queries)
    print(f"== 评测 {len(queries)} 条查询, k={args.k} ==\n")

    # 1) 检索(缓存)
    print("[1/4] 检索...")
    cache = retrieve_cached(["tencent", "baidu"], queries, args.k)

    # 2) BGE 重排器(惰性构建,首次会下载 ~2GB 模型)
    bge = None
    if any(c[2] == "bge" for c in CONFIGS):
        print("\n  加载 BGE-Reranker-v2-m3(首次下载 ~2GB)...")
        bge = BGEReranker()

    # 3) 每 query 跑各配置(orig/rrf/bge),记录策略延迟;收集 judge 池
    print("\n[2/4] 组装配置 + 收集判分池...")
    ranked_by: Dict[str, Dict[str, List[SearchResult]]] = {c[0]: {} for c in CONFIGS}
    rerank_ms: Dict[str, List[float]] = {c[0]: [] for c in CONFIGS}
    pool: Dict[str, Dict[str, str]] = {}  # query -> {url: text}
    for row in queries:
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
        for row in queries:
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
        for p in ["tencent", "baidu"]
    }
    lines = []
    lines.append(f"# IR 评测报告  (queries={len(queries)}, k={args.k})\n")
    lines.append("## 检索延迟(单源平均)")
    lines.append(f"- tencent: {prov_lat['tencent']:.0f} ms")
    lines.append(f"- baidu:   {prov_lat['baidu']:.0f} ms\n")
    lines.append("## 配置对照")
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
    report = "\n".join(lines)
    print(report)
    with open("eval/report.md", "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print("\n→ 已写入 eval/report.md")


def _avg(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


if __name__ == "__main__":
    main()
