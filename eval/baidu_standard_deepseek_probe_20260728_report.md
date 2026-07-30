# 百度标准版 vs 当前方案 vs 高性能版（n=1，k=10）

- generated_at_utc: `2026-07-28T01:52:47Z`
- 当前方案/高性能版基准时间：`2026-07-23T02:23:43Z`
- 标准版模型：`deepseek-v4-flash`
- 标准版配置：`baidu_search_v2`, `search_mode=required`, `enable_deep_search=false`, `stream=true`
- 三方候选 URL 并集由同一 LLM judge 按 0–3 标注；三方答案位置按 Query 确定性盲化

## 检索与排序（三方 URL 并集为 Recall 分母）

| 配置 | nDCG@10 | Recall@10 | P@10 | MRR |
|---|---:|---:|---:|---:|
| current_source | 0.600 | 0.273 | 0.900 | 1.000 |
| current_sf | 0.916 | 0.303 | 1.000 | 1.000 |
| high_baidu | 0.725 | 0.303 | 1.000 | 1.000 |
| high_sf | 0.884 | 0.303 | 1.000 | 1.000 |
| standard_source | 0.197 | 0.030 | 0.100 | 0.100 |
| standard_sf | 1.000 | 0.303 | 1.000 | 1.000 |

### 配对统计

- standard_sf − current_sf nDCG: `0.084`，bootstrap 95% CI `[0.084, 0.084]`，胜/平/负 `[1, 0, 0]`
- standard_sf − current_sf Recall: `0.000`，bootstrap 95% CI `[0.000, 0.000]`，胜/平/负 `[0, 1, 0]`
- standard_sf − high_sf nDCG: `0.116`，bootstrap 95% CI `[0.116, 0.116]`，胜/平/负 `[1, 0, 0]`
- standard_source − standard_sf nDCG: `-0.803`，bootstrap 95% CI `[-0.803, -0.803]`，胜/平/负 `[0, 0, 1]`

- 标准版/当前 URL Jaccard 均值：`0.081`
- 标准版/高性能 URL Jaccard 均值：`0.081`

## 延迟

| 指标 | P50 | P95 |
|---|---:|---:|
| 标准版首引用 | 3136.9 ms | 3136.9 ms |
| 标准版首答案 Token（n=0） | N/A ms | N/A ms |
| 标准版流完成（n=0） | N/A ms | N/A ms |
| 高性能首引用 | 2075.8 ms | 2075.8 ms |
| 高性能流完成 | 2076.4 ms | 2076.4 ms |

## 答案质量：统一生成器对照（三方同场盲评）

为完成全部 1 条来源质量对比，`standard_sf` 使用与 `current_sf` 相同的固定证据回答器；高性能版仍使用其原生答案。本节用于比较可部署管线，不是纯模型对比。

| 配置 | 总分/10 | Correctness/2 | Completeness/2 | Grounding/2 | Freshness/2 |
|---|---:|---:|---:|---:|---:|
| current_sf | 10.000 | 2.000 | 2.000 | 2.000 | 2.000 |
| highperf | 7.000 | 2.000 | 1.000 | 2.000 | 2.000 |
| standard_sf | 9.000 | 2.000 | 2.000 | 2.000 | 2.000 |

- 胜负：`{'current_sf': 1, 'highperf': 0, 'standard_sf': 0, 'tie': 0}`

### 标准版原生答案（仅成功的子样本）

- 标准版原生生成成功/失败：`0/1`
- 流内错误：`account_overdue` / `Access denied due to overdue account`

## 标准版内容与 Token

| 接口 | 引用数 | 平均 content 字符 | 中位数 | P95 |
|---|---:|---:|---:|---:|
| current | 20 | 321.2 | 175.5 | 1009.0 |
| highperf | 20 | 289.6 | 196.0 | 982.0 |
| standard | 20 | 165.8 | 203.0 | 203.0 |

- 标准版 prompt tokens：总计 `0`，成功请求均值 `0.0`
- 标准版 completion tokens：总计 `0`，成功请求均值 `0.0`
- 标准版 total tokens：总计 `0`，成功请求均值 `0.0`
- 其中缓存命中 prompt tokens：`0`
- 按 `deepseek-v4-flash` 公开在线推理单价（输入/缓存输入/输出：`¥0.001/¥0.0002/¥0.002` 每千 Token）估算 0 条模型费用约 `¥0.0000`

## 标准版 QPS 探针

| 目标发送速率 | 请求数 | 成功 | HTTP 状态 | 墙钟耗时 |
|---:|---:|---:|---|---:|
| 未执行 | 0 | 0 | `account_overdue` | N/A |

## 单 Query 明细

| Query | 类型 | 标准引用 | 首引用ms | 总ms | standard_sf nDCG | 答案胜者 |
|---|---|---:|---:|---:|---:|---|
| 三星堆遗址在哪个省 | factual | 20 | 3136.9 | 3137.4 | 1.000 | current_sf |

## 限制

- 当前方案/高性能版沿用 `2026-07-23T02:23:43Z` 的缓存，标准版复测完成于 `2026-07-28T01:52:47Z`；搜索结果具有时变性，因此跨源检索指标不是严格同时点 A/B。
- 原生生成失败 1/1 条；流内错误为 `account_overdue` / `Access denied due to overdue account`。
- 结果只代表 `deepseek-v4-flash`、关闭深搜索的标准版配置；更换模型会改变答案、延迟与费用。
- relevance 与答案评分由单一 LLM judge 完成，尚未人工复核。
- QPS 是四请求短突发，不代表长期吞吐或 SLA。
- 标准版搜索免费额度不覆盖额外的大模型 Token 费用。
