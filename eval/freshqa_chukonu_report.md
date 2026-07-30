# Chukonu FreshQA 评测（n=100）

- generated_at_utc: `2026-07-29T00:51:51.529533+00:00`
- FreshQA snapshot: `2026-04-21` (`3769244f66bb…`)
- split/sample: `TEST` / seed `20260729`
- answer model: `Qwen/Qwen3-30B-A3B-Instruct-2507`
- judge model: `deepseek-ai/DeepSeek-V3.2`
- engine: `http://127.0.0.1:8000/search`, Top-8
- official repo commit: `7d2d3683991916f3633e480548a6aa5c9a62e3db`

## 总览

| 口径 | 无搜索 | Chukonu | 绝对提升 | 95% CI | 胜/平/负 |
|---|---:|---:|---:|---:|---:|
| 严格 | 34.0% | 47.0% | +13.0% | [+6.0%, +21.0%] | 15/83/2 |
| 宽松 | 37.0% | 49.0% | +12.0% | [+4.0%, +20.0%] | 16/80/4 |
| 参考答案字符串命中 | 27.0% | 41.0% | +14.0% | - | - |

## 检索运行

- 成功率：`100.0%`；partial：`15.0%`
- complete：`85.0%`
- 至少一个 provider failure：`15.0%`
- provider failures：`{'tencent:SEARCH_UPSTREAM_REJECTED': 15}`
- 平均 evidence：`7.78`
- 搜索延迟 P50/P95：`1937/2811 ms`

## 分桶（严格）

| fact_type | n | 无搜索 | Chukonu | Δ |
|---|---:|---:|---:|---:|
| fast-changing | 26 | 15.4% | 23.1% | +7.7% |
| never-changing | 37 | 56.8% | 62.2% | +5.4% |
| slow-changing | 37 | 24.3% | 48.6% | +24.3% |

## 明确未过期子集

| 口径 | n | 无搜索 | Chukonu | Δ |
|---|---:|---:|---:|---:|
| 严格 | 95 | 35.8% | 49.5% | +13.7% |
| 宽松 | 95 | 38.9% | 51.6% | +12.6% |

## 任务结构（严格）

| 维度 | 值 | n | 无搜索 | Chukonu | Δ |
|---|---|---:|---:|---:|---:|
| false_premise | FALSE | 84 | 28.6% | 42.9% | +14.3% |
| false_premise | TRUE | 16 | 62.5% | 68.8% | +6.2% |
| num_hops | multi-hop | 21 | 19.0% | 28.6% | +9.5% |
| num_hops | one-hop | 79 | 38.0% | 51.9% | +13.9% |

## 限制

- 官方最新可获取快照早于评测日；样本中有 `5` 条显式 `next_review` 日期已过。
- 自动评分沿用 FreshEval 的严格/宽松原则，但 Judge 不是官方推荐的 GPT-4-1106-preview，分数不可直接用于官方榜单。
- 无搜索与搜索组使用同一回答模型；这能估计搜索增益，但结果仍包含回答模型能力。
- 参考答案字符串命中只作确定性辅助指标，会漏记合法改写。

## 失败样本（严格）

| ID | Question | Baseline | Chukonu |
|---:|---|---:|---:|
| 17 | When was the second time that Joe Biden visited Vietnam during his presidency? | 0 | 0 |
| 59 | When did Joe Biden announce his second presidential campaign in 2023? | 0 | 0 |
| 67 | When was the iPhone 9 released? | 1 | 0 |
| 116 | How long has Harry Styles been a member of One Direction? | 0 | 0 |
| 122 | Who did Luke Humphries beat to win this year's PDC World Darts Championship? | 0 | 0 |
| 127 | Who is the highest-paid coach per season ever on the American television series The Voice? | 0 | 0 |
| 130 | What is the top-ranked university in the US according to the US News Ranking? | 0 | 0 |
| 143 | How many games are there in the Ace Attorney main series?  | 0 | 0 |
| 153 | Which is the most recently launched NASA space telescope that observes in the visible and near-infrared? | 0 | 0 |
| 159 | How many children does Elon Musk have, including his deceased child? | 0 | 0 |
| 161 | How many core UMass NLP faculty are currently primarily affiliated with the lab and have been there for more than five years? | 0 | 0 |
| 163 | How many properties are there in the latest list of World Heritage in danger? | 0 | 0 |
| 165 | What's the largest stadium by capacity in the world? | 0 | 0 |
| 189 | How many food allergens with mandatory labeling are there in the United States? | 0 | 0 |
| 192 | Who is the chancellor of UMass Amherst? | 0 | 0 |
| 201 | What is the largest lottery jackpot for a single ticket in history? | 0 | 0 |
| 210 | How many total Nazca geoglyphs have been discovered so far? | 0 | 0 |
| 220 | Which is the most recent team to advance to consecutive FIFA World Cup finals? | 0 | 0 |
| 224 | Which country has President Joe Biden visited the most during his presidency so far? | 0 | 0 |
| 234 | Who is the vice-chancellor of the largest university by enrollment in the world? | 0 | 0 |
