# Agent 搜索引擎可信证据与陈述校验层设计

> 状态：Phase 0–1 已实现；Phase 2–3 待实现
>
> 参考：`~/Agent-Search-Boundary-PPT-Outline.md`，重点对齐“四条可测能力边界”与“模块三·证据校验与可信输出”
>
> 适用范围：`chukonu-web-search` 的 Web / Academic / Patent evidence、`/search` REST 接口与 MCP 工具
>
> 日期：2026-07-15

## 1. 设计结论

本设计在现有检索引擎中增加独立的 **Trust Layer（证据校验与可信输出层）**，目标不是给网站打一个笼统的信誉分，而是做到：

1. 把研究问题或候选结论拆成最小可验证陈述。
2. 把每条事实性陈述定位到具体文档版本、段落、章节、图表或专利权利要求。
3. 判断原文对陈述是 `支持 / 冲突 / 仅提及 / 证据不足`，不以语义相似代替证据支持。
4. 校验实体、日期、数字、单位、否定、版本、语言和辖区是否一致。
5. 关键陈述要求适配的一手来源或两个独立来源，并主动检索反例与冲突证据。
6. 无充分证据时降级为推断或待专家确认，不允许进入“已支持事实”集合。
7. 输出可复核 locator、原文片段、置信等级和检索边界，供 Agent 生成报告或人工复核。

Trust Layer 是 PPT 三大核心模块中的模块三，必须消费模块一的检索/覆盖状态和模块二的原文结构化结果，并把冲突、缺证和反证检索需求回流模块一。

## 2. 可信能力边界

### 2.1 可操作定义

**可信｜每句可核验**：每条事实性陈述能回到具体版本、段落、图表或权利要求，并区分支持、冲突与证据不足。

这项能力只在明确边界内成立：

- 数据源与索引快照；
- 查询和文档时间截点；
- 语言、地域与法律辖区；
- 全文获取与展示许可；
- 检索轮数、候选数、模型成本和时延预算。

可信表示“证据链可核验”，不等同于结论必然为真；覆盖表示“已检索范围可解释”，不代表穷尽所有相关结果。

### 2.2 能保证与不能保证

Trust Layer 能保证：证据出处、版本、转换过程和 locator 可追溯；陈述与证据关系经过显式校验；冲突和缺口不被隐藏；分析过程带策略和模型版本。

它不能证明来源陈述绝对真实，不能把官网自述当作独立验证，不能把多个搜索 provider 当作多个独立发布者，也不能替代法律、医疗、金融、专利有效性等专业判断。

### 2.3 必须分开的四个概念

| 概念 | 回答的问题 | 输出位置 |
|------|------------|----------|
| Relevance | 文档和 query 是否相关 | `Evidence.scores.relevance` |
| Evidence quality | 证据是否为原文、完整且可定位 | `Evidence.quality` |
| Claim support | 原文是否支持某条具体陈述 | `ClaimAssessment.relations[]` |
| Coverage | 哪些主题、来源和路径已检索，仍缺什么 | `SearchScope/CoverageState` |

相关性高不代表证据支持；来源知名不代表当前版本适用；多条结果不代表多个独立来源。

## 3. 当前实现与主要缺口

### 3.1 可复用基础

当前工程已有：统一 `Evidence`、学术引用与专利元数据、PDF 正文续读、跨源失败诊断、`answerability`、多源召回、URL 去重、领域 reranker，以及 `page/chunk/char` 等初步定位字段。

### 3.2 不能直接宣称“可信”的部分

| 当前行为 | 缺口 |
|----------|------|
| `scores.confidence` 基本等于 relevance | 相关性被误当成可信度 |
| evidence 数量达到 3 即可能 high confidence | 没有判断发布者独立性和证据质量 |
| Web content 来自 provider extract | 未直接核对原网页和版本 |
| Academic 可返回摘要/PDF chunk | 缺少稳定章节、段落、图表 locator |
| Patent 主要返回摘要和著录字段 | 不能定位到权利要求或说明书段落 |
| 搜索接口只接收 query | 没有待验证陈述，无法保证“每句可核验” |
| 当前只找相关结果 | 没有主动挖掘反例和冲突证据 |

因此，MVP 可以先提供 provenance 和证据质量标注，但在原文 locator 与陈述校验完成前，不应把结果标为“已验证事实”。

## 4. 总体架构

### 4.1 目标闭环

```text
模块一：查询规划 / 自适应召回 / 覆盖控制
  → 候选文档、SearchScope、CoverageState
模块二：全文获取 / PDF-OCR 解析 / 版本与专利族归并
  → 带 provenance + locator 的 EvidenceUnit
模块三：陈述拆解 / 证据匹配 / 蕴含与一致性校验
  → supported / conflicted / insufficient + review entry
  → 缺证、冲突、反例查询回流模块一
循环至：高优先级缺口已处理、边际增益饱和或预算耗尽
```

### 4.2 在当前单次 `/search` 管线中的落点

```text
查询理解 → 多源召回 → 相关性重排 → Evidence 构建
  → Provenance/Locator/Quality 标注
  → Evidence-aware Answerability → SearchResponse
```

单次 `/search` 只承诺交付可追溯 evidence，不承诺完成陈述级验证。陈述验证通过独立 `/verify` 或 MCP `verify_claims` 执行；深度 Agent 在两类工具之间循环。

## 5. 输入与输出契约

### 5.1 SearchBoundary

每次 search/verify 都记录评测和解释所需的边界：

```python
class SearchBoundary(BaseModel):
    source_snapshot: dict[str, str]
    query_time: datetime
    languages: list[str]
    jurisdictions: list[str]
    license_scope: list[str]
    max_rounds: int
    max_candidates: int
    deadline_ms: int
```

未明确的项使用服务端实际值，不得输出“已全面检索”等无边界表述。

### 5.2 EvidenceProvenance

`EvidenceProvenance` 至少包含：

- `canonical_url`、`publisher_id/name/type`；
- `retrieved_via`（tencent/baidu/serpapi/openalex_local/patent_es）；
- `content_origin`（original/fulltext/provider_extract/snippet/metadata）；
- `document_id`、`version_id`、`source_record_id`；
- `published_at`、`updated_at`、`retrieved_at`；
- `ownership_group`、`syndication_group`；
- `license`、`original_language`；
- `parser_version`、`ocr_used`、`translation_used`。
- `field_provenance`：关键元数据和抽取字段各自对应的原始记录、转换步骤与版本。

`source` 表示检索 provider，`publisher_id` 表示真正发布者；多个 provider 返回同一文档只算一个发布者。

### 5.3 EvidenceLocator

```python
class EvidenceLocator(BaseModel):
    document_id: str
    version_id: str | None
    section: str | None
    subsection: str | None
    paragraph_id: str | None
    page_from: int | None
    page_to: int | None
    char_start: int | None
    char_end: int | None
    table_id: str | None
    figure_id: str | None
    claim_number: str | None
```

Locator 优先级按领域解释：论文优先章节/段落/页码/图表；专利优先公开号版本、权利要求号和说明书段落；Web 优先 canonical URL、页面版本/时间和段落锚点。

只有 `snippet`、摘要或无 locator 的 evidence 可以用于发现线索，但不能单独把关键陈述判为强支持。

### 5.4 EvidenceUnit

EvidenceUnit 在现有 `Evidence` 上补充：

- 原文最短必要片段 `quote`，与展示用 `passage.text` 分开；
- `locator` 与 `provenance`；
- `content_quality`：`citable/limited/discovery_only/unavailable`；
- `ocr_confidence`、`translation_confidence`，以及机器翻译时的原文；
- `warnings[]`：截断、摘要替代、版本未知、低质量 OCR 等。

遵守来源许可，只存储和展示支撑结论所需的最短证据；许可不允许展示时只返回 locator 和访问入口。

### 5.5 CandidateClaim

```python
class CandidateClaim(BaseModel):
    id: str
    text: str
    claim_type: str
    importance: str              # key/supporting/context
    subject: str | None
    predicate: str | None
    value: str | None
    unit: str | None
    time_scope: str | None
    jurisdiction: str | None
    source: str                  # user/agent/extractor
```

最终报告中的事实句必须先成为 CandidateClaim；纯观点或建议也要标明是推断，不伪装成已验证事实。

### 5.6 ClaimAssessment

每个陈述返回：

- `status`：`supported/conflicted/insufficient/inference/needs_expert_review`；
- `confidence`：`high/medium/low/none`；
- `support_refs[]`、`conflict_refs[]`、`mention_refs[]`；
- 每条关系的 `relation`、locator、最短原文和判定理由；顶层记录本次实际使用的模型/规则版本；
- 实体/日期/数字/单位/否定/版本/辖区检查结果；
- 独立支持来源数、一手来源数、是否执行反证检索；
- `gaps[]`、`followup_queries[]` 和人工复核入口。

响应顶层 `TrustAssessment` 汇总 claim 状态、证据覆盖率、冲突、未验证陈述和 SearchBoundary，但不生成一个伪精确的“真实性总分”。

## 6. 陈述校验流程

### 6.1 陈述拆解

把复合结论拆成最小可验证陈述；一条 claim 只保留一个核心谓词，并显式保留实体、数值、单位、时间、版本和辖区限定。拆解结果必须能重新组合回原句，防止校验时丢失限定条件。

### 6.2 证据召回与原文定位

1. 先在已召回 EvidenceUnit 中按实体、标识符、关键词和语义匹配候选。
2. 优先匹配 original/fulltext，不以 snippet 替代原文。
3. 学术证据定位到 Methods/Results/表格/图；专利定位到具体权利要求或说明书段落。
4. 若候选只有摘要或 locator 缺失，生成模块二的全文/解析请求。
5. 若当前 evidence 不覆盖 claim，生成模块一的补充检索请求。

### 6.3 蕴含关系判定

关系固定为：

- `supports`：原文在保留限定条件后直接支持陈述；
- `contradicts`：原文明确否定、给出不兼容值或证明限定不成立；
- `mentions`：主题相关但不支持该陈述；
- `unclear`：片段、解析或语义不足以判断；
- `irrelevant`：与该陈述无关。

“语义相似度高”只能用于候选召回，不能直接产生 `supports`。

### 6.4 一致性校验

蕴含判定后逐项校验：

| 检查 | 典型错误 |
|------|----------|
| 实体 | 同名公司、材料体系或论文版本混淆 |
| 日期 | 申请日、优先权日、公开日、报道日混用 |
| 数字/单位 | 百万与亿元、质量与体积能量密度混用 |
| 否定/程度 | “未发现”改写为“证实没有”，“可能”改写为“已经” |
| 版本 | 预印本、正式发表、勘误、撤稿或法规旧版本混用 |
| 辖区 | 不同国家法律状态或专利权利范围混用 |
| 翻译/OCR | 识别错误或译文改变技术限定 |

任一关键检查失败，不能保持高置信 `supported`；OCR/翻译证据必须同时展示原文并降低置信度。

### 6.5 一手来源与独立来源规则

来源适配是“来源角色 × 陈述类型”，不是全局网站排名：

- 官方规格可证明厂商“声称了什么”，真实性能仍需独立测评。
- 论文原文可证明作者报告的实验与结论，不自动证明已被独立复现。
- 专利原文可证明公开文本和权利要求，不证明专利有效或技术已商业化。
- 法规和证券披露优先使用对应辖区的正式原文与监管记录。

关键陈述默认要求：一个适配的一手原文直接支持，或两个独立且可定位的来源支持。独立性按 canonical document、publisher/ownership、转载链和引用源计算，不按 provider 或 URL 数量计算。

### 6.6 主动反证与冲突检索

系统不能只在当前 Top-K 中找支持。对 key claim 至少执行一次反证检查：

1. 生成否定、相反结论、旧/新版本、不同辖区和争议关键词。
2. 检索撤稿、勘误、监管更新、无效/终止状态和独立复现。
3. 合并同源转载，避免虚假多数。
4. 发现有效冲突时保留双方最强原文，不在 Trust Layer 内静默选边。
5. 受预算限制未执行时，写入 `COUNTEREVIDENCE_NOT_SEARCHED`。

冲突或证据不足通过 `followup_queries[]` 回流模块一；覆盖控制根据优先级、边际新增证据率和预算决定继续或停止。

### 6.7 状态与置信度

| 状态 | 最低条件 |
|------|----------|
| `supported` | 有可定位证据通过蕴含与一致性检查，并满足该 claim 的来源策略 |
| `conflicted` | 存在至少一条有效支持和一条有效冲突，或高质量来源关键值不一致 |
| `insufficient` | 只有 mention/snippet/摘要、locator 缺失、来源不足或检查无法完成 |
| `inference` | 证据支持前提，但陈述本身包含合理推断；必须展示推断链 |
| `needs_expert_review` | 高风险领域、机器无法消解冲突或专业有效性判断超出边界 |

置信度由 locator 完整度、直接性、一致性、来源策略、独立性、OCR/翻译质量和反证检索完成度共同决定；它不复用 relevance，也不宣称是真实概率。

### 6.8 合格陈述门禁

报告生成器只可把 `supported` 作为无保留事实句；`inference` 必须使用推断措辞，`conflicted` 必须同时描述双方，`insufficient` 必须披露缺口，`needs_expert_review` 必须给出人工复核入口。

这项门禁位于“证据校验之后、报告生成之前”。当前引擎尚不生成报告，但 API/MCP 必须提供足够结构让上游 Agent 执行同一规则。

## 7. Evidence-aware Answerability

当前 `_build_answerability()` 不能再仅按 evidence 数量判断 confidence。搜索级 answerability 先判断证据包是否足以进入陈述验证；claim 级 answerability 由 ClaimAssessment 决定。

新增 gap code：

| code | 触发条件 |
|------|----------|
| `NO_TRACEABLE_SOURCE` | 所有候选均无法追溯 |
| `NO_LOCATOR` | 关键 evidence 无稳定原文定位 |
| `SNIPPET_ONLY` / `ABSTRACT_ONLY` | 只有发现型证据 |
| `CLAIM_TEXT_UNAVAILABLE` | 专利只有摘要，缺少权利要求/说明书原文 |
| `VERSION_UNRESOLVED` | 论文、法规或文档版本未消歧 |
| `JURISDICTION_MISMATCH` | 证据辖区不适用于 claim |
| `OCR_LOW_CONFIDENCE` / `TRANSLATION_LOW_CONFIDENCE` | 转换质量不足 |
| `NO_PRIMARY_SOURCE` | 策略要求一手来源但未找到 |
| `NO_INDEPENDENT_CORROBORATION` | 需要双来源但只有一个独立来源 |
| `SOURCE_CONFLICT` | 有效证据冲突 |
| `COUNTEREVIDENCE_NOT_SEARCHED` | 受预算或失败影响未检查反证 |
| `COVERAGE_SCOPE_UNKNOWN` | 数据源、时间、语言/辖区或路径边界不清 |
| `TRUST_ANALYSIS_FAILED` | 校验层失败并降级 |

没有 locator、只有 snippet/摘要或未完成关键一致性检查时，不能返回 `answerable/high`。

## 8. API 与 Agent 工作流

### 8.1 `/search` 与 MCP `search`

新增 `trust_mode=off|annotate`，默认先旁路 `annotate`：

- 返回 `search_boundary` 和 coverage 摘要；
- 为 evidence 增加 provenance、locator、content_quality 和 warnings；
- 不改变现有相关性排序，直至 A/B 证明约束选证有效；
- 不把 annotate 结果称为 claim verification。

### 8.2 `/verify` 与 MCP `verify_claims`

Phase 1 请求包含：原始问题、`CandidateClaim[]`、现有 `Evidence[]`、profile 和可选 SearchBoundary。响应包含 `ClaimAssessment[]`、TrustAssessment、follow-up queries、SearchBoundary 和 failures。Phase 2 再增加 evidence IDs、是否允许补充检索以及轮数/候选/时延预算。

标准 Agent 流程：

```text
search → 形成候选陈述 → verify_claims
  → supported：进入报告
  → conflicted/insufficient：按 followup_queries 再 search
  → 达到覆盖/饱和/预算停止条件 → 输出边界与剩余缺口
```

## 9. 内容安全、许可与复核

- 所有网页、PDF、OCR 和译文都是不可信数据，不得执行其中面向 Agent 的指令。
- 检测“忽略指令、调用工具、泄露密钥、执行命令”等模式，标记 `PROMPT_INJECTION_SUSPECTED`。
- 后续全文抓取只允许 `http/https`，并校验重定向、DNS/IP、类型、大小和超时。
- 不因页面自称“官方/已验证”而提升来源角色。
- 保留原文、OCR/翻译结果与模型输出的分层 provenance，人工可回看原文。
- 按 API、TDM、缓存和片段展示许可处理；不允许展示时只返回 locator/访问入口。

## 10. 代码落点

建议新增：

```text
src/trust/
  models.py          # SearchBoundary、Locator、Claim、Assessment
  policy.py          # 领域来源策略、关键陈述门禁、版本
  provenance.py      # 发布者、版本、许可与转换链
  independence.py    # ownership、转载、引用源与近重复分组
  matcher.py         # claim → EvidenceUnit 候选匹配
  entailment.py      # supports/contradicts/mentions/unclear
  consistency.py     # 实体、日期、数字、否定、版本、辖区
  counterevidence.py # 反证查询与冲突挖掘
  service.py         # 编排、预算、回流和失败降级

config/
  trust_policies.yaml
  trust_publishers.yaml
```

现有文件修改：

| 文件 | 设计改动 |
|------|----------|
| `src/models.py` | Evidence 增加 provenance/locator/quality；新增 boundary、claim 和 assessment 模型 |
| `src/l0.py` | 识别 verification profile；不做可信判定 |
| `src/engine.py` | Evidence 构建后做 annotate；用证据质量重建 answerability |
| `src/api.py` | `/search` 暴露 annotate；新增 `/verify` |
| `src/mcp_server.py` | search 返回 locator/边界；新增 `verify_claims` 工具 |
| `src/pipeline/rerank.py` | 保持相关性职责，不混入陈述可信度 |

模块二的全文/OCR/专利权利要求解析与证据存储是强可信能力的前置依赖，应单独实现，但接口按本设计的 EvidenceUnit 对齐。

## 11. 失败、预算与降级

- provenance/locator 解析失败：保留 evidence，标为 `discovery_only`，不伪造 locator。
- entailment 模型超时：记录 failure 并降级到保守字面/结构化规则；规则无法直接证明时返回 `insufficient`，不得沿用相似度产生支持。
- 反证检索失败或预算耗尽：保留当前结果，追加缺口和停止原因。
- OCR/翻译失败：优先返回原文入口，必要时转人工复核。
- Trust Layer 整体失败：`/search` 仍返回原 evidence，`failures.stage=trust_analysis`。
- 缓存按 `claim + evidence/version hashes + policy/model/parser version`；时效 claim 不跨时间截点复用。
- 深挖由 `max_rounds/max_candidates/deadline_ms` 和单位预算新增有效证据率共同停止。
- 小模型负责陈述拆解、字段抽取和候选定位；大模型只处理复杂蕴含、冲突分析与综合，按复杂度路由以控制成本。

## 12. 评测设计

### 12.1 统一口径

固定候选源和索引快照、查询时间、语言/辖区、许可、回答模型、最多检索轮数和候选预算。可信评测不能用 retrieval NDCG 代替，召回与精确率继续作为检索护栏。

计划构造 50 条任务：学术 20、专利 20、跨源技术情报 10；最多 6 轮检索、60 条候选。占位指标在正式标注和人审前不得作为效果结论。

### 12.2 核心指标

| 指标 | 定义 |
|------|------|
| 陈述支持精确率 | 判为 supported 的陈述中，人工确认确被原文支持的比例 |
| 证据覆盖率 | 事实性陈述中至少有一条合格 evidence relation 的比例 |
| Locator 准确率 | locator 能否稳定回到正确版本和原文单元 |
| 无依据陈述率 | 报告中无合格支持且未标为推断/缺口的事实句比例 |
| 冲突证据发现率 | 标注有冲突的 claim 中系统发现有效反证的比例 |
| 一致性检查准确率 | 实体/日期/数字/否定/版本/辖区错误识别率 |
| 缺口披露率 | 失败路径、未执行反证和证据不足是否被显式披露 |
| 人工复核可达率 | 引用是否能一跳到原文位置或明确访问入口 |

同时报告延迟、模型成本、平均检索轮数、候选数和降级率，避免只报告质量不报告预算。

### 12.3 必测边界样本

1. 三个 provider 返回同一页面或不同域名转载同一稿件。
2. 论文摘要支持但正文限定不支持，或存在勘误/撤稿。
3. 专利摘要相关但权利要求不覆盖候选技术特征。
4. 申请日、优先权日、公开日及法律状态混淆。
5. 官网规格与独立测试冲突。
6. 新旧法规、不同辖区、不同文档版本冲突。
7. 数字单位、否定、推断强度被改写。
8. OCR/翻译错误和提示注入内容。

### 12.4 MVP 发布门槛

- 没有稳定 locator 的 evidence 不得单独产生高置信 supported。
- snippet/摘要不能替代权利要求、方法、结果或法规原文。
- 检测到有效冲突时不得输出无保留 supported。
- 同一 canonical document/转载链不得计为多个独立来源。
- 反证未检索、版本未消歧和预算停止必须显式披露。
- Trust Layer 失败不得导致 `/search` 整体失败。
- 所有规则、模型、解析器、来源注册表和结果带版本，可复现。

## 13. 分阶段实施

### Phase 0：证据 provenance 与 locator schema（已实现）

- 为现有 Web/Academic/Patent evidence 增加 boundary、provenance、locator 和 content quality。
- 明确 provider extract、摘要、PDF chunk、专利摘要的证据等级。
- annotate 旁路上线，不改排序。

实现位置：`src/trust/annotate.py`；REST/MCP 默认 `trust_mode=annotate`，可用 `off` 回到未标注路径。

### Phase 1：陈述校验 MVP（已实现）

- 新增 `/verify` 和 `verify_claims`。
- 实现 claim 拆解、证据匹配、蕴含关系和一致性检查。
- 先基于现有 evidence 运行；无原文时必须返回 insufficient。

实现位置：`src/trust/claims.py`、`entailment.py`、`verifier.py`；新增 REST `/verify` 与 MCP `verify_claims`。默认有 SiliconFlow key 时使用模型判断，失败降级到保守规则；本阶段不自动补充检索，key claim 显式返回 `COUNTEREVIDENCE_NOT_SEARCHED`。

### Phase 2：原文级定位与反证闭环

- 学术 PDF 解析到章节/段落/图表；专利解析到权利要求/说明书段落。
- 完成版本、专利族、OCR/翻译 provenance。
- verify 的缺证/冲突驱动补充检索，加入覆盖与预算停止。

### Phase 3：报告门禁与校准

- 把 claim status 接入报告生成，执行合格陈述门禁。
- 用 50 条任务和人工复核校准策略、模型、置信等级与领域规则。
- 评测达标后再决定 annotate 默认开启和 verify 自动触发范围。

## 14. 待确认事项

1. 第一阶段优先完成 Academic PDF locator，还是 Patent 权利要求 locator。
2. `/verify` 是否可以自动补充检索，还是只返回 follow-up queries 由 Agent 决定。
3. SearchBoundary 的索引 snapshot/version 能否由三个 provider 稳定提供。
4. 全文、OCR、翻译和最短引文的许可规则由何处统一维护。
5. 哪些 claim/profile 必须进入人工复核，哪些允许双独立来源自动通过。

## 15. 非目标

- 本阶段不把 domain reputation 或被引数包装成真实性概率。
- 不用一个固定线性 `relevance + trust` 总分替代陈述校验。
- 不声称穷尽全网、所有论文、所有专利族或全部冲突证据。
- 不在缺少原文与 locator 时宣称权利要求、实验方法或法规条文已获支持。
- 不隐藏小众来源、反例、冲突、失败路径或预算停止原因。
- 不由 Trust Layer 裁决法律有效性、科学真理或商业决策。
