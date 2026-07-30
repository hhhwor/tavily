# Doubao Search FreshQA 评测（n=100）

- generated_at_utc: `2026-07-29T01:44:21.617695+00:00`
- FreshQA snapshot: `2026-04-21` (`3769244f66bb…`)
- split/sample: `TEST` / seed `20260729`
- answer model: `Qwen/Qwen3-30B-A3B-Instruct-2507`
- judge model: `deepseek-ai/DeepSeek-V3.2`
- engine: `doubao-search-mcp`, Top-8
- official repo commit: `7d2d3683991916f3633e480548a6aa5c9a62e3db`

## 总览

| 口径 | 无搜索 | Doubao Search | 绝对提升 | 95% CI | 胜/平/负 |
|---|---:|---:|---:|---:|---:|
| 严格 | 34.0% | 55.0% | +21.0% | [+10.0%, +32.0%] | 27/67/6 |
| 宽松 | 37.0% | 60.0% | +23.0% | [+12.0%, +34.0%] | 30/63/7 |
| 参考答案字符串命中 | 27.0% | 51.0% | +24.0% | - | - |

## 检索运行

- 成功率：`100.0%`；partial：`0.0%`
- complete：`100.0%`
- 至少一个 provider failure：`0.0%`
- provider failures：`{}`
- 平均 evidence：`8.00`
- 搜索延迟 P50/P95：`1563/3347 ms`

## 分桶（严格）

| fact_type | n | 无搜索 | Doubao Search | Δ |
|---|---:|---:|---:|---:|
| fast-changing | 26 | 15.4% | 34.6% | +19.2% |
| never-changing | 37 | 56.8% | 67.6% | +10.8% |
| slow-changing | 37 | 24.3% | 56.8% | +32.4% |

## 明确未过期子集

| 口径 | n | 无搜索 | Doubao Search | Δ |
|---|---:|---:|---:|---:|
| 严格 | 95 | 35.8% | 56.8% | +21.1% |
| 宽松 | 95 | 38.9% | 62.1% | +23.2% |

## 任务结构（严格）

| 维度 | 值 | n | 无搜索 | Doubao Search | Δ |
|---|---|---:|---:|---:|---:|
| false_premise | FALSE | 84 | 28.6% | 56.0% | +27.4% |
| false_premise | TRUE | 16 | 62.5% | 50.0% | -12.5% |
| num_hops | multi-hop | 21 | 19.0% | 47.6% | +28.6% |
| num_hops | one-hop | 79 | 38.0% | 57.0% | +19.0% |

## 限制

- 官方最新可获取快照早于评测日；样本中有 `5` 条显式 `next_review` 日期已过。
- 自动评分沿用 FreshEval 的严格/宽松原则，但 Judge 不是官方推荐的 GPT-4-1106-preview，分数不可直接用于官方榜单。
- 无搜索与搜索组使用同一回答模型；这能估计搜索增益，但结果仍包含回答模型能力。
- 参考答案字符串命中只作确定性辅助指标，会漏记合法改写。

## 失败样本（严格）

| ID | Question | Baseline | Doubao Search |
|---:|---|---:|---:|
| 17 | When was the second time that Joe Biden visited Vietnam during his presidency? | 0 | 0 |
| 18 | How much did Elon Musk buy Activision Blizzard for? | 0 | 0 |
| 26 | When did Willie Nelson pass away? | 1 | 0 |
| 59 | When did Joe Biden announce his second presidential campaign in 2023? | 0 | 0 |
| 67 | When was the iPhone 9 released? | 1 | 0 |
| 95 | Due to falling electricity prices in January 2023, the Pakistani government pushed back the mandated closing time of shopping malls from 9:30pm to what time of day? | 1 | 0 |
| 116 | How long has Harry Styles been a member of One Direction? | 0 | 0 |
| 122 | Who did Luke Humphries beat to win this year's PDC World Darts Championship? | 0 | 0 |
| 127 | Who is the highest-paid coach per season ever on the American television series The Voice? | 0 | 0 |
| 153 | Which is the most recently launched NASA space telescope that observes in the visible and near-infrared? | 0 | 0 |
| 161 | How many core UMass NLP faculty are currently primarily affiliated with the lab and have been there for more than five years? | 0 | 0 |
| 163 | How many properties are there in the latest list of World Heritage in danger? | 0 | 0 |
| 165 | What's the largest stadium by capacity in the world? | 0 | 0 |
| 194 | What is the most recently released Studio Ghibli film? | 1 | 0 |
| 210 | How many total Nazca geoglyphs have been discovered so far? | 0 | 0 |
| 217 | Who is the oldest Brazilian president at the time of inauguration? | 0 | 0 |
| 220 | Which is the most recent team to advance to consecutive FIFA World Cup finals? | 0 | 0 |
| 234 | Who is the vice-chancellor of the largest university by enrollment in the world? | 0 | 0 |
| 237 | Which was the first country to detect a case of mpox in the most recent outbreak? | 0 | 0 |
| 242 | Who directed the most films in the top 15 highest-grossing films of all time? | 0 | 0 |
