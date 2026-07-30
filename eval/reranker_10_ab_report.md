# Qwen3-Reranker-0.6B vs BGE-Reranker-v2-m3（真实检索 n=10）

- 测试时间：`2026-07-23T08:28:20Z`
- 候选数据：百度标准搜索真实结果，原始抓取时间 `2026-07-23T02:52:39Z`
- 每条 Query 使用同一批最多 20 个候选；相关性标签为既有 0–3 分盲评标签
- Qwen instruction：`Given a web search query, rank passages by whether they directly, correctly, and sufficiently answer the query. For time-sensitive queries, prefer current information. Ignore keyword-only mentions.`

## 汇总

| 模型 | nDCG@10 | Recall@10 | P@5 | MRR | 平均延迟 | P50 | P95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `Qwen/Qwen3-Reranker-0.6B` | 0.8994 | 0.5394 | 0.9400 | 1.0000 | 765.7 ms | 819.2 ms | 855.1 ms |
| `BAAI/bge-reranker-v2-m3` | 0.8687 | 0.5000 | 0.8800 | 1.0000 | 844.0 ms | 784.2 ms | 1450.4 ms |

- nDCG 胜/平/负（Qwen 视角）：`5/2/3`
- Qwen − BGE 平均 nDCG@10：`+0.0307`
- Query 级配对 bootstrap 95% CI：`[-0.0105, +0.0747]`（区间跨 0）

## 分数刻度

| 模型 | 全部文档平均分 | 分数≥0.9 | 分数≥0.99 | rel=1 中位数 | rel=3 中位数 |
|---|---:|---:|---:|---:|---:|
| `Qwen/Qwen3-Reranker-0.6B` | 0.8496 | 70.9% | 41.7% | 0.8735 | 0.9955 |
| `BAAI/bge-reranker-v2-m3` | 0.6572 | 35.7% | 10.1% | 0.2752 | 0.9376 |

Qwen 分数明显更饱和；切换模型时不能沿用 BGE 的固定阈值，需要按业务标签重新校准。

## 单 Query

| Query | 类型 | 候选 | Qwen nDCG | BGE nDCG | 胜者 | Qwen ms | BGE ms |
|---|---|---:|---:|---:|---|---:|---:|
| 三星堆遗址在哪个省 | factual | 20 | 1.0000 | 0.9603 | qwen | 816.0 | 1964.1 |
| 光合作用的基本过程是什么 | factual | 20 | 1.0000 | 0.9405 | qwen | 831.3 | 822.5 |
| 2026年人工智能领域有哪些最新进展 | timely | 19 | 1.0000 | 1.0000 | tie | 855.8 | 802.9 |
| 今天A股大盘行情怎么样 | timely | 20 | 0.8608 | 0.8904 | bge | 631.2 | 577.0 |
| Transformer 和 RNN 在长序列建模上的区别 | multihop | 20 | 0.9552 | 0.7998 | qwen | 854.3 | 604.7 |
| RAG 检索增强生成和模型微调各自的优缺点 | multihop | 20 | 0.8810 | 0.8826 | bge | 834.1 | 813.0 |
| 向量数据库 HNSW 索引的原理 | longtail | 20 | 0.7534 | 0.8451 | bge | 605.6 | 595.0 |
| LangChain 的 agent 是如何调用工具的 | mixed | 20 | 1.0000 | 1.0000 | tie | 806.1 | 783.9 |
| 如何评估搜索引擎的检索质量 | howto | 20 | 0.9423 | 0.8126 | qwen | 822.4 | 784.5 |
| TC3-HMAC-SHA256 签名算法的步骤 | howto | 20 | 0.6011 | 0.5557 | qwen | 600.1 | 692.4 |

## 说明

- 这是小样本先导测试，适合发现明显趋势，不足以给出统计显著结论。
- 标签来自同一 LLM judge，尚未逐条人工复核；两款模型共享该误差来源。
- 延迟包含公网传输和 SiliconFlow 排队时间，不等于模型纯推理时间。
- 两款模型分数刻度不同；质量比较基于排序位置，不比较绝对 relevance_score。
