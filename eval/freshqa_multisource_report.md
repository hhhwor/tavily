# Chukonu 新多源 FreshQA 评测（n=100）

- generated_at_utc: `2026-07-29T02:43:40.591412+00:00`
- FreshQA snapshot: `2026-04-21` (`3769244f66bb…`)
- split/sample: `TEST` / seed `20260729`
- answer model: `Qwen/Qwen3-30B-A3B-Instruct-2507`
- judge model: `deepseek-ai/DeepSeek-V3.2`
- engine: `http://127.0.0.1:8000/search`, Top-8
- official repo commit: `7d2d3683991916f3633e480548a6aa5c9a62e3db`

## 总览

| 口径 | 无搜索 | Chukonu 新多源 | 绝对提升 | 95% CI | 胜/平/负 |
|---|---:|---:|---:|---:|---:|
| 严格 | 34.0% | 58.0% | +24.0% | [+14.0%, +34.0%] | 28/68/4 |
| 宽松 | 37.0% | 60.0% | +23.0% | [+13.0%, +33.0%] | 28/67/5 |
| 参考答案字符串命中 | 27.0% | 45.0% | +18.0% | - | - |

## 检索运行

- 成功率：`100.0%`；partial：`2.0%`
- complete：`98.0%`
- 至少一个 provider failure：`2.0%`
- provider failures：`{'tencent:SEARCH_UPSTREAM_REJECTED': 2}`
- 平均 evidence：`8.00`
- 搜索延迟 P50/P95：`2350/3355 ms`

## 分桶（严格）

| fact_type | n | 无搜索 | Chukonu 新多源 | Δ |
|---|---:|---:|---:|---:|
| fast-changing | 26 | 15.4% | 38.5% | +23.1% |
| never-changing | 37 | 56.8% | 67.6% | +10.8% |
| slow-changing | 37 | 24.3% | 62.2% | +37.8% |

## 明确未过期子集

| 口径 | n | 无搜索 | Chukonu 新多源 | Δ |
|---|---:|---:|---:|---:|
| 严格 | 95 | 35.8% | 60.0% | +24.2% |
| 宽松 | 95 | 38.9% | 62.1% | +23.2% |

## 任务结构（严格）

| 维度 | 值 | n | 无搜索 | Chukonu 新多源 | Δ |
|---|---|---:|---:|---:|---:|
| false_premise | FALSE | 84 | 28.6% | 56.0% | +27.4% |
| false_premise | TRUE | 16 | 62.5% | 68.8% | +6.2% |
| num_hops | multi-hop | 21 | 19.0% | 38.1% | +19.0% |
| num_hops | one-hop | 79 | 38.0% | 63.3% | +25.3% |

## 限制

- 官方最新可获取快照早于评测日；样本中有 `5` 条显式 `next_review` 日期已过。
- 自动评分沿用 FreshEval 的严格/宽松原则，但 Judge 不是官方推荐的 GPT-4-1106-preview，分数不可直接用于官方榜单。
- 无搜索与搜索组使用同一回答模型；这能估计搜索增益，但结果仍包含回答模型能力。
- 参考答案字符串命中只作确定性辅助指标，会漏记合法改写。

## 失败样本（严格）

| ID | Question | Baseline | Chukonu 新多源 |
|---:|---|---:|---:|
| 17 | When was the second time that Joe Biden visited Vietnam during his presidency? | 0 | 0 |
| 59 | When did Joe Biden announce his second presidential campaign in 2023? | 0 | 0 |
| 67 | When was the iPhone 9 released? | 1 | 0 |
| 95 | Due to falling electricity prices in January 2023, the Pakistani government pushed back the mandated closing time of shopping malls from 9:30pm to what time of day? | 1 | 0 |
| 116 | How long has Harry Styles been a member of One Direction? | 0 | 0 |
| 147 | What is the name of the worldwide highest grossing Bollywood movie? | 0 | 0 |
| 153 | Which is the most recently launched NASA space telescope that observes in the visible and near-infrared? | 0 | 0 |
| 161 | How many core UMass NLP faculty are currently primarily affiliated with the lab and have been there for more than five years? | 0 | 0 |
| 165 | What's the largest stadium by capacity in the world? | 0 | 0 |
| 194 | What is the most recently released Studio Ghibli film? | 1 | 0 |
| 201 | What is the largest lottery jackpot for a single ticket in history? | 0 | 0 |
| 210 | How many total Nazca geoglyphs have been discovered so far? | 0 | 0 |
| 220 | Which is the most recent team to advance to consecutive FIFA World Cup finals? | 0 | 0 |
| 224 | Which country has President Joe Biden visited the most during his presidency so far? | 0 | 0 |
| 234 | Who is the vice-chancellor of the largest university by enrollment in the world? | 0 | 0 |
| 237 | Which was the first country to detect a case of mpox in the most recent outbreak? | 0 | 0 |
| 242 | Who directed the most films in the top 15 highest-grossing films of all time? | 0 | 0 |
| 243 | Who bought the most expensive 20th century artwork sold in a public sale? | 0 | 0 |
| 293 | Who was the first football player to score one hundred international goals? | 0 | 0 |
| 295 | What time of day on August 15, 1947 did India become an independent nation? | 0 | 0 |

## 多源组成

- 运行时默认源：`['tencent', 'baidu', 'doubao']`
- evidence 来源标签计数：`{'doubao': 498, 'baidu': 207, 'tencent': 90, 'tencent+baidu': 3, 'doubao+baidu': 2}`
- 拆分合并来源后的 evidence 贡献：`{'doubao': 500, 'baidu': 212, 'tencent': 93}`
- 至少出现一次该 provider 的查询数：`{'doubao': 100, 'baidu': 88, 'tencent': 57}`

## 与既有同样本结果对比

| 对照 | 口径 | 对照结果 | 新多源 | Δ（95% CI） | 胜/平/负 |
|---|---|---:|---:|---:|---:|
| 旧 Chukonu | 严格 | 47.0% | 58.0% | +11.0% ([+2.0%, +20.0%]) | 16/79/5 |
| 旧 Chukonu | 宽松 | 49.0% | 60.0% | +11.0% ([+2.0%, +20.0%]) | 17/77/6 |
| Doubao 单源 | 严格 | 55.0% | 58.0% | +3.0% ([-3.0%, +10.0%]) | 7/89/4 |
| Doubao 单源 | 宽松 | 60.0% | 60.0% | +0.0% ([-6.0%, +6.0%]) | 5/90/5 |

## 运行对比

| 配置 | complete | provider failure | evidence 均值 | P50 | P95 |
|---|---:|---:|---:|---:|---:|
| 旧 Chukonu | 85.0% | 15.0% | 7.78 | 1937 ms | 2811 ms |
| Doubao 单源 | 100.0% | 0.0% | 8.00 | 1563 ms | 3347 ms |
| 新多源 | 98.0% | 2.0% | 8.00 | 2350 ms | 3355 ms |
