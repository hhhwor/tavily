# BrowseComp-ZH 合成冒烟测试

- 时间：`2026-07-30T05:14:54.664845+00:00`
- 模型：`Qwen/Qwen3-30B-A3B-Instruct-2507`
- Judge：`Qwen/Qwen3-30B-A3B-Instruct-2507`
- 四源：`['tencent', 'baidu', 'doubao', 'aliyun']`
- 基础链路结论：**PASS**

## 验收检查

| 检查 | 结果 |
|---|---:|
| `five_synthetic_items` | PASS |
| `health_four_sources` | PASS |
| `leak_filter_self_test` | PASS |
| `judge_self_test` | PASS |
| `b0_completed_5_of_5` | PASS |
| `b0_judged_5_of_5` | PASS |
| `b1_completed_5_of_5` | PASS |
| `b1_judged_5_of_5` | PASS |
| `b2_completed_5_of_5` | PASS |
| `b2_judged_5_of_5` | PASS |
| `b1_exactly_one_search` | PASS |
| `b2_search_used` | PASS |
| `b2_usable_open_url` | PASS |
| `native_schema_without_repair` | PASS |
| `b0_evidence_empty` | PASS |
| `retrieved_answers_have_evidence` | PASS |
| `confidence_uses_percent_scale` | PASS |
| `b0_answered_sanity` | PASS |
| `b1_b2_synthetic_answers_correct` | PASS |
| `no_budget_violation` | PASS |

## 单题结果

| ID | Topic | B0 | B1 | B2 | B2 search/open |
|---|---|---|---|---|---:|
| synthetic-history-001 | 历史 | CORRECT | CORRECT | CORRECT | 1/1 |
| synthetic-geography-001 | 地理 | CORRECT | CORRECT | CORRECT | 1/1 |
| synthetic-music-001 | 音乐 | CORRECT | CORRECT | CORRECT | 1/1 |
| synthetic-science-001 | 医学 | CORRECT | CORRECT | CORRECT | 1/1 |
| synthetic-game-001 | 电子游戏 | NOT_ATTEMPTED | CORRECT | CORRECT | 1/1 |

## 本轮发现

- B0 拒答 `1/5`；带引用 `0/5`。
- B1 回答正确 `5/5`；已回答但无引用 `0/5`。
- B2 回答正确 `5/5`；usable 读页共 `5` 次，非 usable 尝试 `5` 次。
- 全部系统格式修复 `0/15`（其中 B2 `0/5`）；置信度尺度可疑 `0/15`。
- B2 已回答但无引用 `0/5`；引用均由 EvidenceRegistry 的 ref 确定性展开，模型不能自由生成 URL 或 quote。
- 泄漏过滤自检：`PASS`；Judge 自检：`PASS`。
- 只有全部验收检查通过时，才表示当前合成链路达到 Pilot 前置条件。

说明：这是合成题工具链冒烟，不进入 BrowseComp-ZH 正式准确率。
