# BrowseComp-ZH A1–A3 单源合成冒烟测试

- 时间：`2026-07-31T07:08:15.800591+00:00`
- 模型：`Qwen/Qwen3-30B-A3B-Instruct-2507`
- Judge：`Qwen/Qwen3-30B-A3B-Instruct-2507`
- 映射：`A1=Doubao`、`A2=Aliyun`、`A3=Baidu`
- 单源链路结论：**PASS**

## 验收检查

| 检查 | 结果 |
|---|---:|
| `five_synthetic_items` | PASS |
| `health_single_sources_ready` | PASS |
| `leak_filter_self_test` | PASS |
| `judge_self_test` | PASS |
| `a1_completed_5_of_5` | PASS |
| `a1_judged_5_of_5` | PASS |
| `a1_search_used` | PASS |
| `a1_usable_open_url` | PASS |
| `a1_doubao_source_isolated` | PASS |
| `a2_completed_5_of_5` | PASS |
| `a2_judged_5_of_5` | PASS |
| `a2_search_used` | PASS |
| `a2_usable_open_url` | PASS |
| `a2_aliyun_source_isolated` | PASS |
| `a3_completed_5_of_5` | PASS |
| `a3_judged_5_of_5` | PASS |
| `a3_search_used` | PASS |
| `a3_usable_open_url` | PASS |
| `a3_baidu_source_isolated` | PASS |
| `native_schema_without_repair` | PASS |
| `retrieved_answers_have_evidence` | PASS |
| `confidence_uses_percent_scale` | PASS |
| `single_source_synthetic_answers_correct` | PASS |
| `no_budget_violation` | PASS |

## 单题结果

| ID | Topic | A1 Doubao | A2 Aliyun | A3 Baidu |
|---|---|---|---|---|
| synthetic-history-001 | 历史 | CORRECT (1/1) | CORRECT (1/1) | CORRECT (1/1) |
| synthetic-geography-001 | 地理 | CORRECT (1/1) | CORRECT (1/1) | CORRECT (1/1) |
| synthetic-music-001 | 音乐 | CORRECT (1/1) | CORRECT (1/1) | CORRECT (1/1) |
| synthetic-science-001 | 医学 | CORRECT (1/2) | CORRECT (1/1) | CORRECT (1/1) |
| synthetic-game-001 | 电子游戏 | CORRECT (1/1) | CORRECT (1/1) | CORRECT (1/2) |

## 分轨汇总

| 轨道 | 后端 | 正确 | 拒答 | search | usable open | 非 usable open | 格式修复 | 不可恢复搜索错误 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| A1 | doubao | 5/5 | 0/5 | 5 | 6 | 1 | 0/5 | — |
| A2 | aliyun | 5/5 | 0/5 | 5 | 5 | 5 | 0/5 | — |
| A3 | baidu | 5/5 | 0/5 | 5 | 6 | 0 | 0/5 | — |

## 本轮发现

- 每条成功 search 事件均记录 backend 和实际来源；来源隔离门槛要求 A1/A2/A3 分别只能出现 doubao/aliyun/baidu。
- 全部系统格式修复 `0/15`；置信度尺度可疑 `0/15`；已回答但无证据 `0/15`。
- 不可恢复搜索错误：`无`；此类错误终止当前 Agent 的后续搜索，并在无证据时确定性拒答。
- 三轨复用 B2 的规划模型、工具 schema、PageReader、AnswerFinalizer、Judge 与 Standard 预算；单源适配器不经过 Chukonu 四源融合和重排。
- 只有全部验收检查通过时，才表示 A1–A3 单源合成链路可进入 Pilot。

说明：这是合成题工具链冒烟，不进入 BrowseComp-ZH 正式准确率，也不能据此比较单源质量高低。
