# chukonu-web-search FreshQA 专项评测报告

| 文档项 | 内容 |
|---|---|
| 文档编号 | CHUKONU-WS-EVAL-2026-001 |
| 版本 | v1.5（精简版） |
| 报告日期 | 2026-07-29 |
| 已完成范围 | FreshQA 100：Doubao 单源、百度 `/web_summary` 原生对照、当前四源 |
| 未完成范围 | 第 3 节 T02–T12 |

## 1. FreshQA 评测设计

| 项目 | 值 |
|---|---|
| 数据快照 | FreshQA 2026-04-21 |
| 文件 SHA-256 | `3769244f66bb2666fe5160c8cc235339b7c54c61fc88d360995aa91d4c904789` |
| Split / 样本 | TEST / 100 |
| 随机种子 | `20260729` |
| 评测日期 | 2026-07-29 UTC |
| 检索条数 | Web Top-8 |
| 证据预算 | 12,000 字符 |
| 固定回答模型 | `Qwen/Qwen3-30B-A3B-Instruct-2507` |
| Judge | `deepseek-ai/DeepSeek-V3.2` |
| 主指标 | FreshEval 风格 Strict / Relaxed |
| 区间估计 | Wilson 95% CI |

| 组别 | 链路 | 运行批次 |
|---|---|---|
| Doubao 单源 | Doubao Search Top-8 → 固定回答模型 | v1.1 |
| 百度原生对照 | 百度 `/web_summary` 原生 answer | v1.3 |
| 四源 Chukonu（当前默认） | Tencent + Baidu + Doubao + Aliyun WebSearch Top-8 → 固定回答模型 | v1.4 |

百度 `/web_summary` 使用 `stream=true`、`model=non_thinking`、`enable_full_content=true`。四源中的 Aliyun 使用 WebSearch `pro/global`。Doubao 与四源结果包含固定回答模型，百度结果包含百度原生检索与生成能力；三组来自不同运行批次，因此只展示各自结果，不将点估计差值解释为严格配对差异。

## 2. FreshQA 结果

### 2.1 总体结果

| 系统 | Strict（95% CI） | Relaxed（95% CI） | 字符串命中 |
|---|---:|---:|---:|
| Doubao 单源 | 55.0%（45.2%–64.4%） | 60.0%（50.2%–69.1%） | 51.0% |
| 百度 `/web_summary` 原生对照 | 51.0%（41.3%–60.6%） | 53.0%（43.3%–62.5%） | 38.0% |
| **四源 Chukonu（当前默认）** | **60.0%（50.2%–69.1%）** | **64.0%（54.2%–72.7%）** | **54.0%** |

四源在本次保留的三组结果中点估计最高，但 Doubao、百度和四源并非同批严格配对，且百度原生链路包含生成能力，不能据此确认系统间存在统计显著差异。字符串命中只作确定性辅助指标，不替代 Judge。

### 2.2 分桶结果

| Strict 分桶 | n | Doubao 单源 | 百度原生对照 | 四源 Chukonu |
|---|---:|---:|---:|---:|
| fast-changing | 26 | **34.6%** | 23.1% | 30.8% |
| never-changing | 37 | 67.6% | 73.0% | **81.1%** |
| slow-changing | 37 | 56.8% | 48.6% | **59.5%** |
| false-premise=TRUE | 16 | 50.0% | **75.0%** | 68.8% |
| multi-hop | 21 | 47.6% | 42.9% | **61.9%** |

四源在永不变化事实、慢变化事实和多跳题上的点估计最高，但 fast-changing 只有 30.8%。分桶样本较小且未单独做显著性检验，不将点估计解释为稳定总体差异。

### 2.3 四源组成

四源最终返回 800 个 evidence 位置。合并来源会同时归因给多个 provider，因此下表的归因计数之和可以高于 800。

| Provider | evidence 归因计数 | 出现在多少题 |
|---|---:|---:|
| Aliyun | 400 | 100/100 |
| Doubao | 265 | 95/100 |
| Baidu | 110 | 67/100 |
| Tencent | 52 | 38/100 |

Aliyun 出现在全部问题的最终 Top-8，占 400 个 evidence 位置，是当前四源结果中影响最大的新增来源。

### 2.4 运行表现与成本

| 系统 | 延迟口径 | P50/P95 | complete | 平均证据 |
|---|---|---:|---:|---:|
| Doubao 单源 | 搜索 | 1,563/3,347 ms | 100.0% | 8.00 |
| 百度原生对照 | 检索 + 原生生成完整流 | 2,689/3,191 ms | 100.0% | 7.71 |
| **四源 Chukonu** | 搜索 | 3,841/4,801 ms | 100.0% | 8.00 |

- Doubao 单源检索无失败。
- 百度原生 100 次请求均成功，空答案 0、检索重试 0；按 ¥0.060/次估算为 ¥6.00，不抵扣免费额度。
- 四源检索无 provider failure 或 endpoint retry。100 次 Aliyun Pro 检索按 ¥0.042/次估算为 ¥4.20，不含其他来源、固定回答模型与 Judge。
- 百度的延迟包含原生生成，Doubao 与四源只统计搜索；不同口径不得直接等价比较。

### 2.5 结论

- 当前默认四源 Chukonu 的 FreshQA Strict/Relaxed 为 **60.0%/64.0%**。
- Doubao 单源为 55.0%/60.0%，百度原生对照为 51.0%/53.0%；这些是跨批次点估计，不作为严格配对结论。
- 四源 never-changing Strict 为 81.1%，但 fast-changing 只有 30.8%，时效事实仍是主要短板。
- Aliyun 覆盖 100/100 题并占据 400 个最终 evidence 位置；应持续监控来源配额、时效题质量、延迟和成本。
- 本结果用于内部评测，不作为 FreshQA 官方榜单成绩或完整产品 SLA。

## 3. 后续测试模块预留

除 T01 外，以下模块均未执行，结果不得由 FreshQA 推算。

| 模块 ID | 测试模块 | 主要目标 | 数据集/版本 | 样本量 | 主指标 | 结果 | 状态 |
|---|---|---|---|---:|---|---|---|
| T01 | FreshQA | 时效事实、稳定事实与错误前提 | 2026-04-21 | 100 | Strict | Doubao 55.0%；百度原生 51.0%；当前四源 60.0% | 已完成 |
| T02 | SimpleQA | 稳定事实与短答案正确性 | — | — | — | — | 待测 |
| T03 | [BrowseComp-ZH](./browsecomp-zh-test-design.md) | 中文复杂检索与多跳推理 | commit `86abe635` / test | 289 | `Accuracy_valid` | — | 待测（设计完成） |
| T04 | Xbench / 同类复杂检索 | 开放域深度检索 | — | — | — | — | 待测 |
| T05 | 中文 Web 自建集 | 中文时效、地域与来源覆盖 | — | — | — | — | 待测 |
| T06 | 检索相关性 | Recall@K、nDCG、MRR、来源质量 | — | — | — | — | 待测 |
| T07 | 学术搜索 | 论文发现、元数据、引用与时间过滤 | — | — | — | — | 待测 |
| T08 | 专利搜索 | 专利发现、同族、申请人与时间过滤 | — | — | — | — | 待测 |
| T09 | 深度研究 | 查询分解、跨源综合、引文可追溯 | — | — | — | — | 待测 |
| T10 | 稳定性与并发 | 可用率、吞吐、P95/P99、降级与恢复 | — | — | — | — | 待测 |
| T11 | 成本 | 单次查询、单个正确答案与全链路成本 | — | — | — | — | 待测 |
| T12 | 安全与鲁棒性 | 提示注入、恶意网页、错误信息与敏感输出 | — | — | — | — | 待测 |

### 3.1 单模块结果模板

```json
{
  "module_id": "TXX",
  "benchmark": "",
  "status": "pending",
  "evaluation_date": null,
  "dataset": {
    "name": "",
    "version": null,
    "split": null,
    "sample_size": null,
    "sampling_seed": null
  },
  "method": {
    "engine_config": null,
    "answer_model": null,
    "judge": null
  },
  "metrics": {
    "primary": null,
    "engine_value": null,
    "ci95": null
  },
  "runtime": {
    "success_rate": null,
    "complete_rate": null,
    "p50_ms": null,
    "p95_ms": null,
    "p99_ms": null,
    "cost_per_query": null
  },
  "limitations": []
}
```

`status` 允许值为 `pending`、`running`、`completed`、`invalid`。只有 `completed` 的模块才能进入综合结论；`invalid` 必须记录失效原因。

### 3.2 综合结论模板

| 结论项 | 内容 |
|---|---|
| 预先定义的验收阈值 | — |
| 已完成模块 | T01 FreshQA |
| 未完成模块 | T02–T12 |
| 阻断项 | — |
| 风险接受人 | — |
| 最终结论 | 待综合评审 |
