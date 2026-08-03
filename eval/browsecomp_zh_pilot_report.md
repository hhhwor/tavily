# BrowseComp-ZH 55 条 Pilot 运行诊断

- 完成时间：`2026-07-31T10:08:24.243024+00:00`
- Judge 重判时间：`2026-07-31T10:18:55.472752+00:00`
- 数据 SHA-256：`49963cdc8b4a16f4656bbac89ed5f3495f7b3bec4cf310990f567e7893c6a531`
- 固定 commit：`86abe635e7deef89ec00c68ff1c2588f0e2f2099`
- seed / 样本量：`20260730` / `55`
- 运行健康结论：**FAIL**
- 参考答案状态：`official_answers_unaudited`

## 验收检查

| 检查 | 结果 |
|---|---:|
| `selected_55` | PASS |
| `five_per_topic` | PASS |
| `health_four_sources` | PASS |
| `leak_filter_self_test` | PASS |
| `judge_self_test` | PASS |
| `all_165_completed` | FAIL |
| `all_165_judged` | FAIL |
| `b1_exactly_one_search` | PASS |
| `b2_search_used` | FAIL |
| `b2_usable_open` | FAIL |
| `native_schema_without_repair` | PASS |
| `retrieved_answers_have_evidence` | PASS |
| `confidence_uses_percent_scale` | PASS |
| `no_budget_violation` | PASS |
| `no_invalid_open_ref` | PASS |

## 分轨运行

| 轨道 | 完成 | CORRECT | INCORRECT | 拒答 | 模型调用 | search | open | usable | Token | P50 ms | P95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B0 | 55/55 | 2 | 32 | 21 | 87 | 0 | 0 | 0 | 31619 | 3029 | 20163 |
| B1 | 55/55 | 9 | 37 | 9 | 94 | 55 | 0 | 0 | 297652 | 8651 | 28674 |
| B2 | 46/55 | 3 | 38 | 5 | 314 | 47 | 136 | 85 | 2431444 | 51397 | 180972 |

## API 调用量

- 模型调用至少：`608`；Token：`2780513`（含 Judge）。
- Chukonu 逻辑搜索至少：`102`；已记录搜索对应的四源上游请求理论上限：`408`。
- 网页读取至少：`136`。

## Topic 覆盖

| Topic | n | B0 正确 | B1 正确 | B2 正确 |
|---|---:|---:|---:|---:|
| 影视 | 5 | 1 | 2 | 0 |
| 艺术 | 5 | 0 | 0 | 0 |
| 地理 | 5 | 1 | 0 | 0 |
| 音乐 | 5 | 0 | 0 | 0 |
| 历史 | 5 | 0 | 3 | 1 |
| 医学 | 5 | 0 | 0 | 0 |
| 电子游戏 | 5 | 0 | 1 | 0 |
| 科技 | 5 | 0 | 1 | 0 |
| 体育 | 5 | 0 | 0 | 0 |
| 政策法规 | 5 | 0 | 1 | 1 |
| 学术论文 | 5 | 0 | 1 | 1 |

## 限制

- Pilot 只作运行诊断，不发布正式准确率。
- 参考答案尚未完成双人盲化有效性审计。
- 判分为内部 Judge，不是官方 GPT-4o 兼容通道。

明文题目、答案、canary、模型答案与工具轨迹仅保存在受限运行目录，不进入本报告。
