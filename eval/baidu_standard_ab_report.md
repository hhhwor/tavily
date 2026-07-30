# 百度标准版 vs 当前方案 vs 高性能版（n=20，k=10）

- generated_at_utc: `2026-07-23T02:52:39Z`
- 标准版模型：`deepseek-v4-flash`
- 标准版配置：`baidu_search_v2`, `search_mode=required`, `enable_deep_search=false`, `stream=true`
- 三方候选 URL 并集由同一 LLM judge 按 0–3 标注；三方答案位置按 Query 确定性盲化

## 检索与排序（三方 URL 并集为 Recall 分母）

| 配置 | nDCG@10 | Recall@10 | P@10 | MRR |
|---|---:|---:|---:|---:|
| current_source | 0.728 | 0.491 | 0.830 | 0.867 |
| current_sf | 0.892 | 0.529 | 0.890 | 0.955 |
| high_baidu | 0.711 | 0.488 | 0.825 | 0.771 |
| high_sf | 0.890 | 0.540 | 0.905 | 0.967 |
| standard_source | 0.709 | 0.482 | 0.810 | 0.808 |
| standard_sf | 0.883 | 0.538 | 0.900 | 0.967 |

### 配对统计

- standard_sf − current_sf nDCG: `-0.008`，bootstrap 95% CI `[-0.104, 0.098]`，胜/平/负 `[1, 17, 2]`
- standard_sf − current_sf Recall: `0.009`，bootstrap 95% CI `[-0.043, 0.071]`，胜/平/负 `[1, 18, 1]`
- standard_sf − high_sf nDCG: `-0.007`，bootstrap 95% CI `[-0.021, 0.000]`，胜/平/负 `[0, 19, 1]`
- standard_source − standard_sf nDCG: `-0.174`，bootstrap 95% CI `[-0.242, -0.106]`，胜/平/负 `[1, 1, 18]`

- 标准版/当前 URL Jaccard 均值：`0.828`
- 标准版/高性能 URL Jaccard 均值：`0.965`

## 延迟

| 指标 | P50 | P95 |
|---|---:|---:|
| 标准版首引用 | 1535.8 ms | 2399.2 ms |
| 标准版首答案 Token（n=4） | 5466.2 ms | 6296.5 ms |
| 标准版流完成（n=4） | 7667.4 ms | 16874.7 ms |
| 高性能首引用 | 1380.3 ms | 2031.1 ms |
| 高性能流完成 | 4952.3 ms | 7742.5 ms |

## 答案质量：统一生成器对照（三方同场盲评）

标准版20条中只有4条完成原生模型生成。为完成20条来源质量对比，`standard_sf` 使用与 `current_sf` 相同的固定证据回答器；高性能版仍使用其原生答案。

| 配置 | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 |
|---|---:|---:|---:|---:|---:|
| current_sf | 7.950 | 1.850 | 1.800 | 2.000 | 1.850 |
| highperf | 7.800 | 1.850 | 1.800 | 1.900 | 1.800 |
| standard_sf | 6.850 | 1.850 | 1.300 | 1.800 | 1.650 |

- 胜负：`{'current_sf': 7, 'highperf': 10, 'standard_sf': 0, 'tie': 3}`

### 标准版原生答案（仅成功的子样本）

- 有效 Query：`4/20`

| 配置 | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 |
|---|---:|---:|---:|---:|---:|
| current_sf | 8.500 | 2.000 | 2.000 | 2.000 | 1.750 |
| highperf | 7.500 | 2.000 | 1.500 | 2.000 | 1.750 |
| standard_native | 8.750 | 2.000 | 2.000 | 1.750 | 2.000 |

- 胜负：`{'current_sf': 1, 'highperf': 0, 'standard_native': 1, 'tie': 2}`

- 标准版原生生成成功/失败：`4/16`
- 流内错误：`account_overdue` / `Access denied due to overdue account`

## 标准版内容与 Token

| 接口 | 引用数 | 平均 content 字符 | 中位数 | P95 |
|---|---:|---:|---:|---:|
| current | 400 | 700.7 | 764.5 | 1292.0 |
| highperf | 400 | 710.1 | 724.0 | 1305.0 |
| standard | 400 | 188.7 | 203.0 | 203.0 |

- 标准版 prompt tokens：总计 `19895`，成功请求均值 `4973.8`
- 标准版 completion tokens：总计 `3704`，成功请求均值 `926.0`
- 标准版 total tokens：总计 `23599`，成功请求均值 `5899.8`
- 其中缓存命中 prompt tokens：`1024`；按公开单价估算4条模型费用约 `¥0.0265`

## 标准版 QPS 探针

| 目标发送速率 | 请求数 | 成功 | HTTP 状态 | 墙钟耗时 |
|---:|---:|---:|---|---:|
| 未执行（模型账户欠费） | 0 | 0 | `account_overdue` | N/A |

## 单 Query 明细

| Query | 类型 | 标准引用 | 首引用ms | 总ms | standard_sf nDCG | 答案胜者 |
|---|---|---:|---:|---:|---:|---|
| 三星堆遗址在哪个省 | factual | 20 | 3681.6 | 6546.4 | 0.951 | tie |
| 光合作用的基本过程是什么 | factual | 20 | 1495.5 | 16874.7 | 1.000 | current_sf |
| 珠穆朗玛峰的海拔高度是多少 | factual | 20 | 1578.9 | 7667.4 | 0.861 | tie |
| 中国四大发明分别是什么 | factual | 20 | 2108.5 | 11393.0 | 1.000 | tie |
| 2026年人工智能领域有哪些最新进展 | timely | 20 | 2129.2 | 2129.5 | 1.000 | highperf |
| 最近一届诺贝尔物理学奖授予了哪些研究 | timely | 20 | 1493.7 | 1494.0 | 0.960 | current_sf |
| 今天A股大盘行情怎么样 | timely | 20 | 1449.6 | 1449.9 | 0.874 | highperf |
| 本周国际上发生了哪些重大新闻 | timely | 20 | 1535.8 | 1536.2 | 0.333 | current_sf |
| 最新的国产大模型有哪些值得关注 | timely | 20 | 1224.9 | 1225.1 | 0.641 | highperf |
| Transformer 和 RNN 在长序列建模上的区别 | multihop | 20 | 1280.3 | 1280.5 | 0.940 | highperf |
| RAG 检索增强生成和模型微调各自的优缺点 | multihop | 20 | 1485.3 | 1485.5 | 0.892 | highperf |
| 量子计算相比经典计算在哪些问题上有优势 | multihop | 20 | 1483.9 | 1484.2 | 0.924 | current_sf |
| 向量数据库 HNSW 索引的原理 | longtail | 20 | 1628.5 | 1628.9 | 0.881 | current_sf |
| FlashRank 重排序是怎么工作的 | longtail | 20 | 1783.4 | 1783.7 | 0.778 | highperf |
| BGE-Reranker-v2-m3 模型的特点 | longtail | 20 | 2399.2 | 2399.5 | 0.820 | highperf |
| 什么是 Mixture of Experts 架构 | mixed | 20 | 1139.5 | 1139.8 | 1.000 | current_sf |
| LangChain 的 agent 是如何调用工具的 | mixed | 20 | 1542.3 | 1542.6 | 0.964 | highperf |
| 如何评估搜索引擎的检索质量 | howto | 20 | 1606.6 | 1606.9 | 0.939 | current_sf |
| TC3-HMAC-SHA256 签名算法的步骤 | howto | 20 | 1822.2 | 1822.5 | 0.910 | highperf |
| 怎么用 Python 读取一个 JSON 文件 | howto | 20 | 1524.7 | 1524.9 | 1.000 | highperf |

## 限制

- 结果只代表 deepseek-v4-flash、关闭深搜索的标准版配置；更换模型会改变答案、延迟与费用。
- 标准版第5条起因账户欠费只返回搜索引用，原生答案层有效样本为4；QPS探针因此未继续消耗模型调用。
- relevance 与答案评分由单一 LLM judge 完成，尚未人工复核。
- QPS 是四请求短突发，不代表长期吞吐或 SLA。
- 标准版搜索免费额度不覆盖额外的大模型 Token 费用。
