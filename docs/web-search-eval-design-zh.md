# 中文 Web 搜索能力横向评测设计方案

> 评测对象：本项目面向 Agent 的搜索引擎，**仅评 web 检索能力**（不含 academic / patent 支线）。
>
> 对标对象：博查 Bocha AI Search、智谱 Web Search Pro、Tavily（中文）。
>
> 场景：**中文为主**（含少量英文控制组）。
>
> 状态：设计方案，本文只定义方法、指标、数据集规范与复现口径，不含实现代码。
>
> 日期：2026-07-09

---

## 1. 目标与非目标

**目标**：在中文真实场景下，量化本引擎（多源 `tencent + baidu + serpapi` + SiliconFlow 重排）的 web 检索质量，并与博查 / 智谱 / Tavily 做**同 query、同 top_k、同判分池、同 rubric** 的公平横向对比，给出可复现、可发布的结论。

**非目标**：
- 不评 academic / patent 支线（已有独立报告）。
- 不做全网排行榜刷分；主分只作参考，不作唯一结论（沿用现报告口径）。
- 本方案默认不改变引擎线上行为，只新增评测代码路径。

---

## 2. 比什么：两个层次

| 层次 | 比较单元 | 回答的问题 | 复用现有 |
|------|----------|-----------|---------|
| **L1 检索质量 (IR)** | 各引擎返回的 Top-K 结果列表 | 谁召回准、排序好、源更优 | `eval/run_eval.py` + `eval/judge.py` + `eval/metrics.py` |
| **L2 回答质量 (端到端 RAG)** | 各引擎结果喂给**同一** answer agent + judge 的最终答案 | 换成这个搜索源，agent 最终答对了吗 | `eval/agent_answer_eval.py` / `eval/e2e_judge.py` |

**优先级**：L1 为核心、成本低、先落地。L2 为可选扩展，只在 `timely / factual / multihop` 三桶抽样跑（这三桶最能体现"对 agent 有用"）。本方案两层都定义，实施时按资源决定是否做 L2。

---

## 3. 对标对象与适配

竞品统一封装为与 `src/providers/base.py` 同接口的 adapter（`.search(query, k) -> List[SearchResult]`），原始结果落盘缓存，评测层不感知引擎差异。

| 引擎 | adapter | 缓存文件 | 说明 |
|------|---------|---------|------|
| **本引擎（主配置）** | 现有 `triple+sf` 组装 | 复用 `search_tencent/baidu/serpapi.json` | 多源 + SiliconFlow 重排 |
| 本引擎（消融）| `triple+rrf` / `triple_minus_overlap+sf` | 同上 | 见 §7 公平性 |
| 博查 Bocha | `bocha.py`（新增） | `search_bocha.json` | agent-first 中文 web API，最直接对标 |
| 智谱 Web Search Pro | `zhipu.py`（新增） | `search_zhipu.json` | 开放平台 web_search 工具 |
| Tavily（中文）| `tavily.py`（新增） | `search_tavily.json` | 国际 agent 搜索 baseline，你们已参考其评测方法 |

字段映射到统一 `SearchResult`（`title / url / snippet / content / publish_date?`）。缺字段置空并在报告标注"该引擎不提供 X"。

---

## 4. 数据集设计（中文场景）

现有 `eval/dataset.jsonl` 的 web 部分仅 30 条，桶不够中文化。目标 **150–300 条，分层**，拆成两个文件：

- `eval/web_golden_zh.jsonl`：**静态 golden 集**，带参考答案 `answer`（用于事实/时效题的对错判定），可复现。
- `eval/web_fresh_zh.jsonl`：**滚动时效集**，用 web-eval-generator 当天生成；时效题隔天即失效，必须与竞品**同日**采集。

每条 schema：

```json
{"query": "...", "scenario": "timely", "answer": "...(可选,事实/时效题必填)", "recency_days": 7}
```

分桶（加粗为中文差异最易暴露处）：

| scenario | 说明 | 目标条数 |
|----------|------|---------|
| **timely** 时效 | 新闻/股市/赛事/政策，依赖官媒、百家号等本土源 | 30（走 fresh 集） |
| factual 事实 | 中文 SimpleQA 式短可验证答案 | 30 |
| **ecosystem** 中文生态 | 答案主要在知乎/公众号/小红书/B站/百家号 | 25 |
| longtail/domain 长尾垂类 | 技术/医疗/法律/金融 | 30 |
| **colloquial** 口语化 | 真实用户口语 query，考中文意图理解 | 20 |
| multihop 多跳 | 需跨源综合 | 15 |
| **local** 本地生活 | 城市/店铺/服务 | 15 |
| entity 实体消歧/简繁 | 简繁体、拼音、同名实体 | 15 |
| english 控制组 | 排除"只是中文强" | 10 |

构造要求：真实来源采样（勿全用榜单热词）、去重、避免答案泄漏在 query 中、时效题标注 `recency_days`。

---

## 5. 指标

### 5.1 检索质量（沿用现有）
`NDCG@10 / Recall@10 / P@10 / MRR`，pooled 判分（见 §6）。

### 5.2 中文 web 特有（新增到 `eval/metrics.py`）

| 指标 | 定义 | 意义 |
|------|------|------|
| `source_diversity` | Top-K 唯一域名数 | 结果集中度 |
| `zh_ecosystem_coverage` | 命中"中文优质源清单"（知乎/公众号/官媒/百家号…）的比例 | 中文生态触达 |
| `freshness_hit` | timely 桶中，结果 `publish_date` 落在 `recency_days` 窗内的比例 | 时效硬指标 |
| `dead_link_rate` | 死链 / SEO 农场 / 纯导航页比例（抽检 HEAD + 规则） | 结果有效性 |
| `content_richness` | 返回正文可用长度（**单独统计，不进相关性**） | 各 API 给全文/仅 snippet 差异 |

### 5.3 回答级（L2，可选）
`SimpleQA CORRECT/INCORRECT/NOT_ATTEMPTED`、Faithfulness、Citation precision、Coverage。

### 5.4 延迟
p50 / p95。本引擎多源必然更慢——单独出"质量 vs 延迟"散点，与竞品同框、诚实呈现。

---

## 6. 判分设计（公平是命根子）

- **Pooled relevance**：每条 query 把**所有引擎**的 Top-K 并成共享池，judge 只判一次，每引擎在**同池、同 rubric**下算分。现有 `run_web_vs_academic_coverage()` 已是此模式，直接推广到多引擎。
- **Rubric**：复用 `eval/judge.py` 的中文 `_RUBRIC`（0–3 级"是否回答查询"）。
- **盲评**：判分输入剥离"结果来自哪个引擎"。
- **文本对齐**：judge 统一喂 `title + snippet`，避免"给全文的引擎"占相关性便宜；`content_richness` 单列。
- **时效/事实题**：用 golden 集 `answer` 做对错判定（reference-based），不仅相关性。
- **偏差校准**：10–20% 样本人工抽检；可选双 judge（Claude + 另一模型）算 Cohen's κ 报一致性。

---

## 7. 公平性红线（必须在报告显式声明）

1. **主场优势**：本引擎的 `triple+sf` **已包含百度+腾讯**。因此"本引擎 vs 百度/腾讯单源"只证明"多源+重排的增量"，**不得**包装成"击败百度"。干净的外部对标是 vs 博查/智谱/Tavily。
2. **重叠源消融**：增设 `triple_minus_overlap+sf` 配置（剔除与竞品明显重叠的源）以示公道。
3. **时效同日采集**：所有引擎在同一时间窗采集并冻结原始结果，杜绝时间漂移。
4. **top_k 归一**：所有引擎取同一 k（默认 10）。
5. **snippet vs 全文**：见 §6 文本对齐。
6. **query 泄漏/去重**：数据集侧已控。

---

## 8. 报告结构

1. Executive Summary（总榜 = 各桶 NDCG@10 宏平均 + 关键结论）。
2. 分桶明细表（重点看 timely / ecosystem / colloquial / longtail——中文差异最大处）。
3. 中文特有指标表（生态覆盖 / 时效命中 / 死链率 / 内容丰富度）。
4. 质量 vs 延迟散点。
5. L2 回答级结果（若做）。
6. **公平性声明**章节（§7 全部披露）。
7. 失败模式定性分析（抽典型 badcase）。
8. 复现命令 + 冻结的缓存/判分产物清单。

主分（参考，不作唯一结论）：

```
Web Search Score =
  0.55 * RetrievalQuality(桶宏平均 NDCG@10)
+ 0.20 * FreshnessHit(时效桶)
+ 0.15 * ZhEcosystemCoverage
+ 0.10 * LatencyScore(按业务预算归一)
```

---

## 9. 落地清单（全部是对现有框架的加法，本方案不实现）

1. `src/providers/`：新增 `bocha.py` / `zhipu.py` / `tavily.py`，实现 `base.py` 同接口，结果落 `eval/cache/search_<engine>.json`。
2. `eval/`：新增 `web_golden_zh.jsonl` + `web_fresh_zh.jsonl`（带 `scenario` / `answer` / `recency_days`）。
3. `eval/run_eval.py`：`CONFIGS` 从"内部策略"扩为"引擎矩阵"；判分池改为跨引擎 pooled；按 `scenario` 分桶聚合。
4. `eval/metrics.py`：新增 `source_diversity` / `zh_ecosystem_coverage` / `freshness_hit` / `dead_link_rate` / `content_richness`。
5. （可选 L2）`eval/agent_answer_eval.py`：以"搜索源"为变量，跑 `timely/factual/multihop` 抽样对比。
6. 中文优质源清单：新增一份 `eval/zh_source_whitelist.txt`（知乎/公众号/官媒/百家号/B站…域名）。

---

## 10. 复现口径（实现后）

```bash
cd /home/ec2-user/tavily

# L1 多引擎横向（中文 golden + fresh，分桶）
.venv311/bin/python -m eval.run_eval --dataset eval/web_golden_zh.jsonl --engines ours,bocha,zhipu,tavily

# 时效集当天单独采集（避免时间漂移）
.venv311/bin/python -m eval.run_eval --dataset eval/web_fresh_zh.jsonl --engines ours,bocha,zhipu,tavily --freeze-today

# L2 回答级抽样（可选）
.venv311/bin/python -m eval.agent_answer_eval --dataset eval/web_golden_zh.jsonl --scenarios timely,factual,multihop --engines ours,bocha,zhipu,tavily
```

> 注：`--engines` / `--dataset` / `--freeze-today` 为本方案建议新增的参数，当前 `run_eval.py` 尚未实现。

---

## 参考

- 现有报告：`docs/agent-search-eval-report.md`、`docs/eval-methodology.md`
- Tavily Search Evals: https://github.com/tavily-ai/tavily-search-evals
- Tavily Web Eval Generator: https://docs.tavily.com/examples/use-cases/web-eval
- OpenAI SimpleQA: https://openai.com/index/introducing-simpleqa/
- Exa Eval Philosophy: https://exa.ai/blog/evals-at-exa
</content>
</invoke>
