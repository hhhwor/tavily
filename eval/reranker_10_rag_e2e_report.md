# 四款 Reranker 端到端 RAG 答案测试（n=10，Top-5）

- generated_at_utc: `2026-07-23T09:14:02Z`
- answer/judge model: `claude-haiku-4-5-20251001`
- 回答器、提示词、候选池和证据数量完全一致；只改变 Reranker 的 Top-5 及顺序
- 四答案对 Judge 匿名，位置按 Query 确定性打乱

## 答案质量

| Reranker | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 | 胜次 |
|---|---:|---:|---:|---:|---:|---:|
| `Qwen/Qwen3-Reranker-8B` | 7.600 | 1.900 | 1.700 | 1.900 | 1.900 | 0 |
| `Qwen/Qwen3-Reranker-4B` | 7.600 | 1.900 | 1.800 | 1.900 | 1.700 | 0 |
| `Qwen/Qwen3-Reranker-0.6B` | 7.700 | 1.800 | 1.800 | 1.900 | 1.800 | 4 |
| `BAAI/bge-reranker-v2-m3` | 7.500 | 1.900 | 1.500 | 1.900 | 1.800 | 1 |
| `tie` | - | - | - | - | - | 5 |

## Top-5 证据集合重合度

| 模型对 | 平均 Jaccard | 完全相同 Query 数 |
|---|---:|---:|
| `qwen8b` vs `qwen4b` | 0.695 | 3/10 |
| `qwen8b` vs `qwen` | 0.492 | 0/10 |
| `qwen8b` vs `bge` | 0.587 | 1/10 |
| `qwen4b` vs `qwen` | 0.554 | 0/10 |
| `qwen4b` vs `bge` | 0.612 | 3/10 |
| `qwen` vs `bge` | 0.466 | 0/10 |

## 引用审计

| Reranker | 有引用答案 | 越界引用 | 平均使用证据数 | 平均答案字符 |
|---|---:|---:|---:|---:|
| `Qwen/Qwen3-Reranker-8B` | 10/10 | 0 | 3.90 | 360.6 |
| `Qwen/Qwen3-Reranker-4B` | 10/10 | 0 | 3.50 | 372.6 |
| `Qwen/Qwen3-Reranker-0.6B` | 10/10 | 0 | 3.90 | 380.3 |
| `BAAI/bge-reranker-v2-m3` | 10/10 | 0 | 4.10 | 363.6 |

## 延迟

| Reranker | 回答 P50 | 回答 P95 | Rerank+回答 P50 | Rerank+回答 P95 |
|---|---:|---:|---:|---:|
| `Qwen/Qwen3-Reranker-8B` | 4903.4 ms | 5647.0 ms | 6287.9 ms | 7029.5 ms |
| `Qwen/Qwen3-Reranker-4B` | 5367.6 ms | 7082.4 ms | 8684.7 ms | 15678.1 ms |
| `Qwen/Qwen3-Reranker-0.6B` | 5319.8 ms | 6502.1 ms | 6052.4 ms | 7245.4 ms |
| `BAAI/bge-reranker-v2-m3` | 5345.9 ms | 7222.8 ms | 6084.0 ms | 8015.6 ms |

## 单 Query

| Query | 类型 | 8B | 4B | 0.6B | BGE | 胜者 |
|---|---|---:|---:|---:|---:|---|
| 三星堆遗址在哪个省 | factual | 9 | 9 | 9 | 8 | tie |
| 光合作用的基本过程是什么 | factual | 9 | 8 | 9 | 8 | tie |
| 2026年人工智能领域有哪些最新进展 | timely | 8 | 8 | 9 | 9 | qwen |
| 今天A股大盘行情怎么样 | timely | 7 | 7 | 4 | 7 | tie |
| Transformer 和 RNN 在长序列建模上的区别 | multihop | 8 | 8 | 9 | 7 | qwen |
| RAG 检索增强生成和模型微调各自的优缺点 | multihop | 8 | 8 | 8 | 9 | bge |
| 向量数据库 HNSW 索引的原理 | longtail | 8 | 8 | 8 | 8 | tie |
| LangChain 的 agent 是如何调用工具的 | mixed | 8 | 8 | 9 | 8 | qwen |
| 如何评估搜索引擎的检索质量 | howto | 8 | 8 | 8 | 8 | tie |
| TC3-HMAC-SHA256 签名算法的步骤 | howto | 3 | 4 | 4 | 3 | qwen |

## 限制

- 只有10条中文 Web Query，不能代表英文、论文、专利或企业知识库。
- 相关性标签和答案评分均由模型完成，尚未人工复核。
- 回答器与 Judge 使用同一模型，可能存在共享偏好；匿名与位置轮换只能缓解部分偏差。
- 回答请求并发执行，延迟包含代理、排队与公网波动；端到端延迟为不同阶段测量值之和。
