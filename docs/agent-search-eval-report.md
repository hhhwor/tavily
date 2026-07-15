# Agent 搜索引擎评测报告（草案）

> 评测对象：本项目面向 Agent 的搜索引擎，包括 `/search` REST 接口、`/mcp` 工具、Web/Academic/Patent 多源 evidence 输出、`answerability` 诊断和 PDF 正文续读能力。
>
> 报告日期：2026-07-08
>
> 当前状态：本报告基于仓库现有评测输出与一次小规模学术/专利动态 QA 生成试跑。它可作为正式报告模板；正式发布前需要补齐外部对照、LLM judge 和人工抽检。

## 1. Executive Summary

当前评测显示，本引擎在 **结构化 evidence 输出、专利检索、Web 重排质量** 上已经具备可交付基础：

- **Web 检索主配置**：三源 + SiliconFlow 重排在 30 条 Web 查询上达到 `NDCG@10=0.906`、`P@10=0.963`，显著优于三源 RRF 的 `NDCG@10=0.774`。
- **专利支线**：12 条专利查询上 `patent+sf` 达到 `NDCG@10=0.993`、`Recall@10=1.000`、`P@10=0.983`；即使不重排，`patent+noop` 也有 `NDCG@10=0.986`，说明底层 ES 召回质量较强。
- **学术支线**：12 条学术查询上 `academic+sf` 达到 `NDCG@10=0.879`、`Recall@10=0.917`，但 `P@10=0.658`，说明召回覆盖较好，Top-K 纯净度仍需优化。
- **端到端路由**：6 条 E2E 样本中 academic/patent/timely 路由准确率均为 `1.000`，p95 延迟为 `3921 ms`。
- **MCP Tool Agent**：1 条技术情报场景中工具调用率、所需来源覆盖率、缺口披露率均为 `1.000`，但样本量过小，不能作为质量结论。

主要风险：

- 当前正式 judge 在 E2E、Agent 场景和 Tool Agent 报告里关闭，`Answer Accuracy / Faithfulness / Citation Precision` 尚未形成可发布结论。
- 学术补盲对比使用 Web rubric 时，`academic-only` 分数低于 `web-only`，说明必须补一套学术专用 rubric，不能用通用 Web 判断直接替代。
- 动态 QA 试跑仅 6 条，且为结构化字段抽取题，适合作冒烟，不足以代表真实 Agent 搜索质量。

## 2. 评测方法选择

我们采用“报告优先”的组合评测，而不是单一排行榜分数。原因是本引擎不仅返回网页结果，还面向 Agent 返回结构化 evidence、引用、专利元数据、PDF 状态和可答性诊断。

借鉴方法：

| 参考 | 可借鉴做法 | 本报告采用方式 |
|------|------------|----------------|
| Tavily Search Evals | SimpleQA + Document Relevance，多 provider 横向对比，结果落盘和 resume | 短答案 QA + 文档相关性 + 可复现输出目录 |
| Tavily Web Eval Generator | 从实时 Web 生成动态 QA 数据集 | 已试跑 Chukonu academic/patent 动态 QA 样本 |
| OpenAI SimpleQA | 短事实答案，分 `CORRECT / INCORRECT / NOT_ATTEMPTED` | 后续用于最终回答准确率 |
| OpenAI BrowseComp | 难找但易验证，短答案，多步浏览能力 | 后续构造少量技术情报 hard set |
| Linkup Complex Query Eval | Source diversity、Faithfulness、Entity coverage | 用于技术尽调/多实体任务 |
| Exa Eval Philosophy | Query-result pair 由 LLM judge 打相关性/质量分 | 沿用现有 LLM-as-judge IR |
| Valyu Agent Search Eval | Agent-first、domain-specific、结构化上下文、信息密度 | 单独报告 academic/patent 和 evidence schema |

## 3. 数据与产物

当前报告引用的本地评测产物：

| 文件 | 内容 |
|------|------|
| `eval/report.md` | Web/Academic/Patent IR 指标 |
| `eval/e2e_report.md` | `SearchEngine.search()` 端到端路由与延迟 |
| `eval/agent_scenario_report.md` | 技术尽调场景 full agent vs baidu-only |
| `eval/tool_agent_report.md` | MCP tool-calling agent E2E 冒烟 |
| `/home/ec2-user/tavily-web-eval-generator/datasets/chukonu_academic_patent_eval_6_qa_2026-07-08_05-24-10.json` | 小规模学术/专利动态 QA 样本 |

动态 QA 试跑样本：

| Type | Question | Answer |
|------|----------|--------|
| academic | What DOI is listed for the academic work titled "Sulfide Solid Electrolyte with Favorable Mechanical Property for All-Solid-State Lithium Battery"? | `10.1038/srep02261` |
| academic | What DOI is listed for the academic work titled "Interfacial phenomena in solid-state lithium battery with sulfide solid electrolyte"? | `10.1016/j.ssi.2012.01.009` |
| academic | What DOI is listed for the academic work titled "An All‐Solid‐State Battery Based on Sulfide and PEO Composite Electrolyte"? | `10.1002/smll.202202069` |
| patent | What publication number is listed for the patent titled "SODIUM-ION BATTERY CATHODE MATERIAL"? | `WO-2024020042-A1` |
| patent | What publication number is listed for the patent titled "SODIUM-ION BATTERY CATHODE MATERIAL"? | `EP-4559031-A1` |
| patent | What publication number is listed for the patent titled "Sodium ion battery cathode material"? | `CN-120051872-A` |

## 4. Retrieval Quality

### 4.1 Web

| 配置 | NDCG@10 | Recall@10 | P@10 | MRR | rerank_ms |
|------|---------|-----------|------|-----|-----------|
| tencent | 0.844 | 0.381 | 0.880 | 0.983 | 0 |
| baidu | 0.753 | 0.399 | 0.900 | 0.936 | 0 |
| serpapi | 0.482 | 0.241 | 0.633 | 0.704 | 0 |
| triple+rrf | 0.774 | 0.394 | 0.880 | 0.983 | 0 |
| **triple+sf** | **0.906** | **0.442** | **0.963** | **0.983** | 5767 |
| triple+sf+fusion | 0.895 | 0.442 | 0.963 | 0.983 | 5693 |

结论：

- `triple+sf` 是当前 Web 质量主配置。
- Fusion 没有带来增益，且略降 NDCG，应继续默认关闭。
- SerpAPI 单源弱，但作为多源补召回可保留，正式报告需补 source diversity 分析。

### 4.2 Patent

| 配置 | NDCG@10 | Recall@10 | P@10 | MRR | rerank_ms |
|------|---------|-----------|------|-----|-----------|
| patent+noop | 0.986 | 1.000 | 0.983 | 1.000 | 0 |
| **patent+sf** | **0.993** | **1.000** | **0.983** | **1.000** | 1737 |
| patent+sf+thr | 0.955 | 0.940 | 0.933 | 1.000 | 1762 |

结论：

- 专利 ES 的原始召回已经很强；重排提供小幅排序提升。
- 阈值过滤会误伤专利结果，当前不应对专利支线启用强过滤。
- 正式报告应增加结构化字段完整率：`publication_number/application_number/applicant/inventor/ipc_main/cpc_main/status/family_id`。

### 4.3 Academic

| 配置 | NDCG@10 | Recall@10 | P@10 | MRR | rerank_ms |
|------|---------|-----------|------|-----|-----------|
| academic+noop | 0.832 | 0.917 | 0.658 | 0.917 | 0 |
| **academic+sf** | **0.879** | **0.917** | **0.658** | **0.917** | 2872 |
| academic+sf+thr | 0.577 | 0.476 | 0.375 | 0.833 | 2884 |

结论：

- 学术支线的召回覆盖较好，重排提升 NDCG。
- 阈值过滤明显有害，不应作为默认。
- `P@10=0.658` 表示 Top-K 仍有噪声；下一步要优化学术 query rewrite 和领域 rerank 特征。

## 5. Agent Readiness

当前 evidence schema 已经覆盖 Agent 需要的主要字段：

- `passage.text`：可直接引用的证据文本。
- `citation`：学术引用和专利公开号引用。
- `patent`：专利专属结构化元数据。
- `access`：OA、license、PDF 状态、`next_cursor`。
- `diagnostics`：截断、部分失败和 evidence 级失败原因。
- `answerability`：整体可答性、缺口和置信度。

正式报告应增加以下统计：

| 指标 | 计算方式 |
|------|----------|
| citation_complete_rate | academic evidence 中 DOI/work_id/year/venue 至少 3 项非空比例 |
| patent_complete_rate | patent evidence 中 publication_number/applicant/date 至少 3 项非空比例 |
| passage_support_rate | `passage.text` 非空且长度大于阈值的比例 |
| pdf_ready_rate | academic evidence 中 `access.pdf_status=ready` 的比例 |
| answerability_gap_precision | 系统报告缺口时，人工/LLM judge 判断缺口真实存在的比例 |

## 6. End-to-End 与 MCP

### 6.1 In-process E2E

| Metric | Value |
|--------|-------|
| academic_route_acc | 1.000 |
| patent_route_acc | 1.000 |
| timely_route_acc | 1.000 |
| p95 latency | 3921 ms |
| latency score | 1.000 |

当前只评了 6 条，且 judge 关闭。该结果只能说明路由冒烟通过，不能说明回答质量。

### 6.2 Agent Scenario

| Metric | full_agent | baidu_only |
|--------|------------|------------|
| avg_latency_ms | 16006 | 1334 |
| p95_latency_ms | 19127 | 1373 |
| avg_web_evidence | 8.0 | 6.5 |
| avg_academic_evidence | 4.0 | 0.0 |
| avg_patent_evidence | 8.0 | 0.0 |

结论：

- full agent 能返回 Web + Academic + Patent 多模态 evidence，覆盖明显强于 baidu-only。
- 延迟显著更高，正式报告必须把质量增益和延迟成本放在同一张表里。
- 当前 Answer winner/Judge 为空，需要打开 answer pair judge 后才能下结论。

### 6.3 MCP Tool Agent

| Metric | Value |
|--------|-------|
| tool_call_rate | 1.000 |
| avg_tool_calls | 4.00 |
| avg_required_source_coverage | 1.000 |
| total_support_audit_flags | 0 |
| gap_disclosure_rate | 1.000 |
| p95_elapsed_ms | 53082 |

结论：

- MCP 工具链可以被 Agent 正常调用。
- 仅 1 条样本，不能代表稳定性。
- `p95_elapsed_ms=53082` 偏高，后续报告应分解为 search latency、LLM latency、tool loop latency。

## 7. 报告评分口径

正式报告建议给出一个主分，但不把主分当唯一结论：

```
Agent Search Score =
  0.30 * RetrievalQuality
+ 0.25 * AnswerAccuracy
+ 0.20 * Faithfulness
+ 0.15 * Coverage
+ 0.10 * LatencyScore
```

其中：

- `RetrievalQuality`：Web/Academic/Patent 分桶 NDCG@10 宏平均。
- `AnswerAccuracy`：SimpleQA/BrowseComp-like 短答案 `CORRECT` 比例。
- `Faithfulness`：最终回答中被 citation/evidence 支撑的原子事实比例。
- `Coverage`：所需来源、实体、子问题覆盖率。
- `LatencyScore`：按业务预算归一化，例如 p95 <= 8s 记满分，超过 30s 记 0。

## 8. 正式报告还缺的数据

| 缺口 | 影响 | 补齐方式 |
|------|------|----------|
| E2E judge 关闭 | 无法报告最终回答准确率 | 打开 `eval/run_e2e_eval.py` 的 judge |
| Agent pair judge 关闭 | 无法比较 full agent vs baidu-only | 打开 `eval/run_agent_scenario_compare.py` judge |
| MCP 样本只有 1 条 | 不能评估 tool stability | 扩到 10-20 条技术情报场景 |
| 动态 QA 只有 6 条 | 只能冒烟 | 扩到 academic 20 + patent 20 |
| 无外部 provider 横向 | 无法对标 Tavily/Exa/Brave | 接入外部 API 或先报告内部对照 |
| 无人工抽检 | LLM judge 偏差无法校准 | 抽 10%-20% 样本人工复核 |

## 9. 复现命令

```bash
cd /home/ec2-user/tavily

# IR 评测
.venv311/bin/python -m eval.run_eval

# E2E 冒烟，不调 judge
.venv311/bin/python -m eval.run_e2e_eval --no-judge

# Agent 场景对比
.venv311/bin/python -m eval.run_agent_scenario_compare --no-judge

# MCP tool agent 冒烟
.venv311/bin/python -m eval.run_tool_agent_eval --no-judge --max-scenarios 1

# 动态 academic/patent QA 生成（在克隆的 Tavily web-eval 工具中）
cd /home/ec2-user/tavily-web-eval-generator
SEARCH_PROVIDER_TIMEOUT=60 .venv/bin/python scripts/generate_chukonu_eval_questions.py --num-qa 6 --top-k 3
```

## 10. 下一步

建议按以下顺序把草案升级为正式报告：

1. 扩充动态 QA：academic 20 条、patent 20 条、technical intelligence 10 条。
2. 打开 answer judge，输出 `correct/incorrect/not_attempted` 和 faithfulness。
3. 增加 evidence schema 统计，展示结构化字段完整率。
4. 对 MCP tool agent 跑 10-20 条，分解 tool loop 延迟。
5. 选择是否加入外部 API 对照；若加入，必须固定同一 query、top_k、时间、answer agent 和 judge。
6. 对 10%-20% 失败/边界样本人工复核，写入失败模式章节。

## 参考

- Tavily Search Evals: https://github.com/tavily-ai/tavily-search-evals
- Tavily Web Eval Generator: https://docs.tavily.com/examples/use-cases/web-eval
- OpenAI SimpleQA: https://openai.com/index/introducing-simpleqa/
- OpenAI BrowseComp: https://openai.com/index/browsecomp/
- Linkup Complex Query Eval: https://www.linkup.so/blog/evaluating-ai-search-systems-on-complex-queries
- Exa Eval Philosophy: https://exa.ai/blog/evals-at-exa
- Valyu Agent Search Benchmark: https://www.valyu.ai/blogs/benchmarking-search-apis-for-ai-agents
