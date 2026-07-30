# Qwen3-Reranker-4B vs 0.6B vs BGE（真实检索 n=10）

- 4B 测试时间：`2026-07-23T08:56:56Z`
- 0.6B/BGE 原测试时间：`2026-07-23T08:28:20Z`
- 候选数据抓取时间：`2026-07-23T02:52:39Z`
- 三款模型使用完全相同的每条最多 20 个真实搜索候选和 0–3 相关性标签
- Qwen instruction：`Given a web search query, rank passages by whether they directly, correctly, and sufficiently answer the query. For time-sensitive queries, prefer current information. Ignore keyword-only mentions.`

## 汇总

| 模型 | nDCG@10 | Recall@10 | P@5 | MRR | 平均延迟 | P50 | P95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `Qwen/Qwen3-Reranker-4B` | 0.9050 | 0.5411 | 0.9200 | 1.0000 | 3918.8 ms | 2542.4 ms | 10636.5 ms |
| `Qwen/Qwen3-Reranker-0.6B` | 0.8994 | 0.5394 | 0.9400 | 1.0000 | 765.7 ms | 819.2 ms | 855.1 ms |
| `BAAI/bge-reranker-v2-m3` | 0.8687 | 0.5000 | 0.8800 | 1.0000 | 844.0 ms | 784.2 ms | 1450.4 ms |

## 配对比较

- 4B − 0.6B nDCG@10：`+0.0056`，bootstrap 95% CI `[-0.0159, +0.0304]`，胜/平/负 `4/3/3`
- 4B − BGE nDCG@10：`+0.0363`，bootstrap 95% CI `[+0.0037, +0.0770]`，胜/平/负 `7/2/1`
- 0.6B − BGE nDCG@10：`+0.0307`，bootstrap 95% CI `[-0.0105, +0.0747]`，胜/平/负 `5/2/3`

- 4B P50 延迟是 0.6B 的 `3.10×`，是 BGE 的 `3.24×`。

## 分数刻度

| 模型 | 全部文档平均分 | 分数≥0.9 | 分数≥0.99 | rel=1 中位数 | rel=3 中位数 |
|---|---:|---:|---:|---:|---:|
| `Qwen/Qwen3-Reranker-4B` | 0.6921 | 33.7% | 1.0% | 0.4319 | 0.9187 |
| `Qwen/Qwen3-Reranker-0.6B` | 0.8496 | 70.9% | 41.7% | 0.8735 | 0.9955 |
| `BAAI/bge-reranker-v2-m3` | 0.6572 | 35.7% | 10.1% | 0.2752 | 0.9376 |

## 单 Query

| Query | 类型 | 4B nDCG | 0.6B nDCG | BGE nDCG | 最优 | 4B ms |
|---|---|---:|---:|---:|---|---:|
| 三星堆遗址在哪个省 | factual | 1.0000 | 1.0000 | 0.9603 | tie | 10180.9 |
| 光合作用的基本过程是什么 | factual | 0.9432 | 1.0000 | 0.9405 | qwen | 2512.1 |
| 2026年人工智能领域有哪些最新进展 | timely | 1.0000 | 1.0000 | 1.0000 | tie | 11009.2 |
| 今天A股大盘行情怎么样 | timely | 0.9027 | 0.8608 | 0.8904 | qwen4b | 1962.6 |
| Transformer 和 RNN 在长序列建模上的区别 | multihop | 0.9603 | 0.9552 | 0.7998 | qwen4b | 4098.8 |
| RAG 检索增强生成和模型微调各自的优缺点 | multihop | 0.8575 | 0.8810 | 0.8826 | bge | 2643.0 |
| 向量数据库 HNSW 索引的原理 | longtail | 0.8466 | 0.7534 | 0.8451 | qwen4b | 1009.5 |
| LangChain 的 agent 是如何调用工具的 | mixed | 1.0000 | 1.0000 | 1.0000 | tie | 2093.5 |
| 如何评估搜索引擎的检索质量 | howto | 0.9488 | 0.9423 | 0.8126 | qwen4b | 2572.7 |
| TC3-HMAC-SHA256 签名算法的步骤 | howto | 0.5908 | 0.6011 | 0.5557 | qwen | 1105.5 |

## 限制

- n=10 是先导样本；配对置信区间跨 0 时不能认定模型差异稳定显著。
- 标签来自同一 LLM judge，尚未逐条人工复核。
- 三款模型不是同一时刻调用；延迟只能看量级和中位数，不能做精确性能基准。
- relevance_score 刻度不同，固定阈值需要按模型分别校准。
