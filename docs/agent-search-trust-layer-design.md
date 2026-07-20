# Agent 轻量搜索与可信研究任务接口设计

> 状态：`search.v1`、持久化 SearchSeed、ResearchTask 状态机与首版 ResearchDossier 已实现；全文 locator、版本/专利族归并和覆盖驱动扩展继续增强
>
> 参考：`~/Agent-Search-Boundary-PPT-Outline.md`、`~/DeepScope-PPT-Outline.md`，重点对齐“轻量搜索”“可验证覆盖”和“持续下钻的研究闭环”
>
> 适用范围：`chukonu-web-search` 的 Web / Academic / Patent evidence、REST `/search` 与 `/research`、MCP `search` 与 `research`
>
> 日期：2026-07-17

## 1. 设计结论

公开用户能力收敛为两个边界清晰的接口：

1. **`/search` 是轻量搜索服务**：秒级、单轮、无研究任务状态，返回相关且可追溯的首批 evidence。它负责“找到什么”，不负责证明结论成立。
2. **`/research` 是可信研究任务接口**：以 `/search` 的 `search_id` 为种子，异步执行全文深读、论文版本/专利族归并、引用与同族扩展、陈述校验、主动反证、覆盖缺口补搜和饱和停止。它负责“这些证据足以说明什么、还缺什么”。
3. 原 `/verify`、`verify_claims`、REST PDF route 和 `get_pdf_text` 已从公开契约删除；其逻辑分别下沉为 `/research` 的“陈述校验”和“全文深读”内部阶段。
4. `/research` 的目标不是给网站或答案生成一个笼统的真实性分数，而是交付可定位、可交叉验证、可复现并明确边界的研究档案。

研究层必须做到：

- 把研究问题或候选结论拆成最小可验证陈述；
- 把事实性陈述定位到具体文档版本、段落、章节、图表或专利权利要求；
- 判断原文对陈述是 `支持 / 冲突 / 仅提及 / 证据不足`，不以语义相似代替证据支持；
- 校验实体、日期、数字、单位、否定、版本、语言和辖区；
- 关键陈述要求适配的一手来源或两个独立来源，并主动检索反例与冲突证据；
- 按研究成果和专利族归并独立结果，不把多 provider、论文版本或同族成员虚增为多份证据；
- 展示覆盖矩阵、数据缺口、停止原因和真实执行边界；
- 无充分证据时返回推断、证据不足或待专家确认，而不是提高措辞确定性。

因此，“提升可信度”的含义是提升证据的独立性、可定位性、覆盖度、一致性与可复现性，或诚实暴露这些维度无法提升；它不保证研究结论一定更乐观，也不等同于证明结论绝对真实。

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

Research Layer 能保证：证据出处、版本、转换过程和 locator 可追溯；陈述与证据关系经过显式校验；冲突和缺口不被隐藏；分析过程带策略和模型版本。

它不能证明来源陈述绝对真实，不能把官网自述当作独立验证，不能把多个搜索 provider 当作多个独立发布者，也不能替代法律、医疗、金融、专利有效性等专业判断。

### 2.3 必须分开的四个概念

| 概念 | 回答的问题 | 输出位置 |
|------|------------|----------|
| Relevance | 文档和 query 是否相关 | `Evidence.scores.relevance` |
| Evidence quality | 证据是否为原文、完整且可定位 | `Evidence.quality` |
| Claim support | 原文是否支持某条具体陈述 | `ClaimAssessment.relations[]` |
| Coverage | 哪些主题、来源和路径已检索，仍缺什么 | `RetrievalBoundary/ResearchDossier.coverage` |

相关性高不代表证据支持；来源知名不代表当前版本适用；多条结果不代表多个独立来源。

## 3. 当前实现与主要缺口

### 3.1 可复用基础

当前工程已有：统一 `Evidence`、学术引用与专利元数据、PDF 正文续读、跨源失败诊断、`answerability`、多源召回、URL 去重、领域 reranker、`page/chunk/char` 等初步定位字段，以及可复用的 claim 拆解、蕴含关系和一致性校验内核。

当前公开实现已经收敛：

- REST：`POST /search` 与 `/research` 任务资源族；
- MCP：`search` 与统一生命周期工具 `research`；
- Verify 与 PDF 续读只作为 ResearchService 的内部校验、深读构件；
- `/search` 固定执行基础 Evidence 标注，不同步触发 PDF 或 claim verification。

首版 `/research` 已具备持久化 task、固定 seed 复制、多轮补搜、PDF 深读、主动反证、claim 校验、coverage/dossier 与停止原因；稳定全文 locator、论文版本/专利族归并和更多扩展路径仍需继续增强。

### 3.2 改造前的风险与当前剩余边界

| 改造前行为 | 为何不能直接宣称“可信” |
|----------|------|
| `scores.confidence` 基本等于 relevance | 相关性被误当成可信度 |
| evidence 数量达到 3 即可能 high confidence | 没有判断发布者独立性和证据质量 |
| Web content 来自 provider extract | 未直接核对原网页和版本 |
| Academic 可返回摘要/PDF chunk | 缺少稳定章节、段落、图表 locator |
| Patent 主要返回摘要和著录字段 | 不能定位到权利要求或说明书段落 |
| `/search` 同时暴露检索、PDF 和 trust 高级参数 | 轻量搜索边界不清，首用成本和延迟不可预测 |
| `/verify` 要求客户端回传完整 Evidence | 传输冗余，且服务端可信字段不能由客户端声明 |
| PDF 续读只返回裸 text/page/cursor | 后续正文不能原样进入 claim 校验和证据图谱 |
| 三个接口由客户端手工编排 | 没有任务状态、研究轮次、覆盖控制和统一停止条件 |
| 当前只找相关结果 | 没有主动挖掘反例和冲突证据 |

上述公开契约与编排问题已经通过 `search.v1/research.v1` 改造；当前 `/research` 也已执行原文深读、陈述校验、主动反证和覆盖停止。但稳定全文 locator、论文版本/专利族归并以及更广扩展路径仍未完成，因此首版 dossier 仍不应被解读为“穷尽性研究”。

## 4. 总体架构

### 4.1 两层用户架构

```text
POST /search
  查询理解 → 单轮多源召回 → 去重 → 相关性排序 → 轻量 Evidence
  → research_seed + RetrievalBoundary + retrieval assessment

POST /research {search_id, objective, scope, policy, budget}
  创建 ResearchTask
  → 规划覆盖维度与候选陈述
  → 通过 SearchService 执行多路补充检索
  → 全文/PDF/OCR/权利要求深读
  → 论文版本与专利族归并、独立来源分组
  → claim-evidence 蕴含与一致性校验、主动反证
  → CoverageMatrix 与信息增益判断
  → 循环至覆盖目标满足、边际增益饱和或预算停止
  → ResearchDossier
```

`/research` 是应用编排层，不直接实现第二套 provider、排序或全文网关。每轮扩展都复用与 `/search` 相同的 SearchService、SourceRegistry 和 Evidence schema，并记录实际查询、过滤条件、来源快照和失败。

### 4.2 `/search` 目标边界

`/search` 的目标是“低认知成本地返回首批相关证据”：

- 单轮、同步、秒级；
- 最小请求只需 `query`，常用控制只有全局 `limit`、`source_types` 和 `filters`；
- 不同步下载或循环续读 PDF，不主动生成反证查询，不做跨轮覆盖控制；
- 返回基础 provenance、文档身份、发现型 passage、可用性和失败诊断；
- 正常返回 `research_seed` 和 `RetrievalBoundary`，供 `/research` 在服务端读取不可变的首批 evidence 快照；
- `retrieval_assessment` 只描述召回包的覆盖与可用性，不把它命名为事实已 `answerable/high`；
- 单次 `/search` 失败或部分失败不创建隐式研究任务。

### 4.3 `/research` 目标边界

`/research` 的目标是“把首批搜索结果转成可检查、可复现、明确边界的研究结论”：

- 有状态、异步、多轮；
- 必须从服务端持有的 `search_id` 启动，默认不接受客户端自报的 provenance、locator 或 quality；
- 根据 objective/profile 解析必要技术特征、claim、日期口径、语言和辖区；
- 对关键结果深读原文，按研究成果和专利族归并，并区分发布者独立性；
- 对 key claim 执行原文级支持、冲突和反证检查；
- 根据未覆盖的特征、来源、辖区、分类和关系路径继续调用 SearchService；
- 输出 finding、证据漏斗、覆盖矩阵、可信维度、执行轨迹、停止原因和剩余缺口；
- 可以返回比 `/search` 更低的确定性；“可信度提升”不意味着强行提高 confidence。

### 4.4 内部阶段与旧接口映射

| 现有能力 | `/research` 内部阶段 | 目标公开状态 |
|----------|----------------------|--------------|
| 原 `/verify` / `verify_claims` | claim decomposition、entailment、consistency、source policy | 仅内部阶段，公开入口已删除 |
| 原 `get_pdf_text` / REST PDF route | deep reading、content continuation、locator assembly | 仅内部阶段，公开入口已删除 |
| `/search include_pdf_text=*` | seed discovery；research 决定哪些文档值得深读 | 从 canonical `/search` 请求移除 |
| `trust_mode=annotate` | 轻量 Evidence 基础标注 | 变成服务端固定行为，不再要求普通用户选择 |

## 5. 输入与输出契约

### 5.1 RetrievalBoundary 与 ResearchBoundary

`/search` 只记录本次单轮检索真实发生的边界：

```python
class RetrievalBoundary(BaseModel):
    source_snapshot: dict[str, str]
    query_time: datetime
    languages: list[str]
    jurisdictions: list[str]
    license_scope: list[str]
    candidate_limit: int
    deadline_ms: int
```

`/research` 聚合 seed 边界、resolved scope 与实际多轮预算：

```python
class ResearchBoundary(BaseModel):
    seed_boundary: RetrievalBoundary
    resolved_scope: ResearchScope
    max_rounds: int
    max_candidates: int
    max_deep_reads: int
    deadline_ms: int
    rounds_executed: int
    actual_queries: list[ExecutedQueryRef]
```

`deadline_ms` 在 `/search` 表示同步请求 deadline，在 `/research` 表示任务进入 `running` 后的执行 deadline；排队时间不计入执行 deadline，另受服务端最大排队时长约束。未明确的项使用服务端实际值，不得输出“已全面检索”等无边界表述。

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

### 5.5 CandidateClaimInput 与 CandidateClaim

```python
class CandidateClaimInput(BaseModel):
    text: str
    importance: Literal["key", "supporting", "context"] = "key"
    time_scope: str | None = None
    jurisdiction: str | None = None

class CandidateClaim(CandidateClaimInput):
    id: str
    claim_type: str
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    unit: str | None = None
    source: Literal["user", "agent", "extractor"] = "user"
```

`objective.claims[]` 接受最小 `CandidateClaimInput`；服务端规划后补齐 ID、类型、结构化字段和来源，形成不可变 CandidateClaim。最终报告中的事实句必须先成为 CandidateClaim；纯观点或建议也要标明是推断，不伪装成已验证事实。

### 5.6 ClaimAssessment

每个陈述返回：

- `status`：`supported/conflicted/insufficient/inference/needs_expert_review`；
- `confidence`：`high/medium/low/none`；
- `support_refs[]`、`conflict_refs[]`、`mention_refs[]`；
- 每条关系的 `relation`、locator、最短原文和判定理由；顶层记录本次实际使用的模型/规则版本；
- 实体/日期/数字/单位/否定/版本/辖区检查结果；
- 独立支持来源数、一手来源数、是否执行反证检索；
- `gaps[]`、`followup_queries[]` 和人工复核入口。

`ClaimAssessment` 作为 `/research` 的内部与输出子结构，不能单独代表研究任务完成。研究响应还必须汇总覆盖、独立性、原文可定位性、冲突、复现轨迹和停止原因，但不生成一个伪精确的“真实性总分”。

### 5.7 SearchSeed

目标 `/search` 响应新增服务端持有的研究种子：

```python
class SearchSeed(BaseModel):
    search_id: str
    created_at: datetime
    expires_at: datetime
    evidence_count: int
    seed_snapshot_hash: str
```

`SearchResponse.research_seed` 的类型是 `SearchSeed | null`；检索边界始终位于响应顶层的 `retrieval_boundary`。`search_id` 唯一标识不可变的 evidence 与边界快照，`seed_snapshot_hash` 同时承诺两者的内容。`/research` 只按 `search_id` 读取服务端证据，避免客户端回传大体积 Evidence，也避免调用方伪造可信字段。若部署必须无状态，可以使用有签名、可过期的 `search_context_token`，但不得让客户端修改 token 内的 evidence identity、provenance 或 quality。

### 5.8 ResearchRequest

真正的最小请求只包含：

```json
{
  "search_id": "srch_123"
}
```

完整请求：

```python
class ResearchRequest(BaseModel):
    search_id: str
    profile: Literal[
        "literature_review",
        "technology_validation",
        "prior_art_landscape",
        "technology_landscape",
    ] = "technology_validation"
    depth: Literal["quick", "standard", "deep"] = "standard"
    objective: ResearchObjective | None = None
    scope: ResearchScope | None = None
    policy: ResearchPolicyRef | None = None
    budget: ResearchBudget | None = None
    privacy: ResearchPrivacy | None = None
```

参数解析顺序固定为 `profile defaults → depth budget preset → explicit objective/scope → policy registry → explicit budget cap → safety hard limits`。响应中的 `resolved` 是唯一实际执行口径，至少包含 normalized objective、resolved scope、seed 纳入/排除统计及原因、profile、depth、policy、budget、privacy 和 `adjustments[]`；被服务端收紧或拒绝的参数必须显式返回，不能静默改写。

其中：

- `objective`：研究问题、可选 CandidateClaim、必要技术特征和用户指定核心种子；未传时继承 `/search` query 并由研究规划器拆解；
- `scope`：来源类型、起止时间及日期口径、语言、辖区、许可和必须覆盖的分类；未传时继承 seed 边界，显式 scope 对后续研究轮次优先，但不修改原 seed；不符合新 scope 的 seed evidence 标为 excluded 并记录原因；
- `profile` 决定研究路径与默认 policy，`depth` 决定默认预算；两者都省略时使用 `technology_validation + standard`；
- `policy`：普通调用者只能选择服务端 registry 中的版本化策略 ID，如 `technical-evidence.v1`，不能逐项降低独立来源、全文或反证门槛；响应返回 resolved policy；
- `budget`：`max_rounds/max_candidates/max_deep_reads/deadline_ms`，显式值只能收紧 depth preset 和账户硬上限；响应返回 resolved budget 与被截断项；
- `privacy`：`standard/restricted` 和是否允许外部模型处理原文；受限模式默认禁止外发。

`detail` 是读取偏好，不参与任务身份、执行或缓存。REST 使用 `GET /research/{id}?detail=standard|full`，MCP 在 `operation=get` 时传入 `detail`。两种级别都内嵌所有 finding 引用的最小 EvidenceUnit；`full` 额外返回未引用的采纳 evidence、完整查询轨迹和可用 artifact 元数据。

`prior_art_landscape` 只生成现有技术证据、日期和特征矩阵，不生成侵权、有效性、FTO 或可专利性法律结论。

### 5.9 ResearchTask

研究任务的生命周期与当前执行阶段分开：

```python
class ResearchTask(BaseModel):
    research_id: str
    state: Literal[
        "queued", "running", "completed", "partial",
        "needs_input", "failed", "cancelled",
    ]
    phase: Literal[
        "planning", "expanding", "deep_reading", "normalizing",
        "verifying", "coverage_analysis", "synthesizing",
    ] | None
    seed_search_id: str
    seed_snapshot_hash: str
    evidence_set_revision: int
    task_revision: int
    created_at: datetime
    updated_at: datetime
```

对外 TaskEnvelope 在所有状态保持相同字段集合：`ResearchTask + resolved + progress + input_request + dossier + stop + failures + links + retry_after_ms`；当前状态不适用的字段返回 `null`，不通过改变 response shape 表达状态。`task_revision` 每次状态/进度提交递增，`evidence_set_revision` 只在新 evidence set 原子提交后递增。

进度不使用无法校准的百分比，而是公开实际漏斗计数：完成轮数、原始候选数、独立研究成果数、专利族数、深读数、采纳 evidence 数和剩余 gap 数。

### 5.10 ResearchDossier

完成或部分完成的研究任务返回 `ResearchDossier`：

- `findings[]`：候选结论、claim status、置信等级、合格支持/冲突/反证引用和局限；
- `assessment`：覆盖、独立性、可定位性、一致性、来源质量和可复现性等分维度状态；
- `evidence_funnel`：原始候选 → 独立研究成果/专利族 → 深读 → 采纳 evidence；
- `coverage.matrix[]`：技术特征、来源类型、语言、辖区、时间、分类和关系路径的覆盖状态；
- `coverage.gaps[]`：结构化 code、severity、message、retryable 和 suggested action；
- `boundaries`：实际来源与快照、查询日志、日期口径、语言/辖区、许可、处理策略和所有 limitations；
- `artifacts`：文献地图、专利全景、证据图谱、审计轨迹和完整结果库的引用。

任务级 `stop` 与 `failures[]` 位于稳定 TaskEnvelope，而不嵌入 dossier：这样 `failed/cancelled` 即使没有 dossier，也能表达停止原因与诊断。dossier 中的 gap 通过稳定 ID 被 `stop.remaining_gap_refs` 引用。

顶层研究结论只使用：

- `sufficient`；
- `sufficient_with_limitations`；
- `insufficient`；
- `conflicted`；
- `needs_expert_review`。

不输出单一 `trust_score`。不同维度不可用固定线性权重合成为“真实性概率”，所有 rate 必须标明是本次任务的 observed metric，而不是把产品目标值包装成测量结果。

## 6. 陈述校验流程

本节是 `/research` 的内部阶段规范，不对应新的公开 `/verify` 能力。调用方只提交研究目标并消费 ResearchDossier，不需要自行编排 claim、全文和 evidence。

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
4. 发现有效冲突时保留双方最强原文，不在 Research Layer 内静默选边。
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

## 7. 分层评估语义

### 7.1 `/search` RetrievalAssessment

`/search` 不再以 `answerability.status/confidence` 暗示问题已经可以可靠作答。轻量搜索只返回 `retrieval_assessment`：

- `status`：`usable/limited/unusable`；
- `coverage`：本次请求期望的 source type 是否有返回；
- `quality_mix`：`citable/limited/discovery_only/unavailable` 数量；
- `gaps[]`：缺失来源、部分失败、正文不可用、过滤未应用等检索级诊断。

它回答“这批搜索结果是否可作为进一步研究的种子”，不回答任何具体事实是否成立。顶层 `status=complete/partial` 只描述执行是否完整；成功执行但零结果仍是 `complete`，由 `retrieval_assessment.status=unusable` 与 `NO_EVIDENCE` gap 表达结果为空。按 evidence 数量推导的旧 `answerable/high` 字段已从公开响应删除。

### 7.2 `/research` ResearchAssessment

研究级评估必须基于实际 claim、locator、来源独立性、反证和覆盖矩阵。至少分开：

| 维度 | 回答的问题 |
|------|------------|
| Coverage | 必要 claim、特征、来源、辖区和时间范围覆盖了多少 |
| Independence | 去重后有多少独立研究成果、发布者或专利族 |
| Traceability | 支持结论的引用能否回到具体原文单元 |
| Consistency | 支持、冲突、反证、版本和数值检查是否一致 |
| Source quality | 是否使用适配的一手、官方、授权或全文来源 |
| Reproducibility | 查询、过滤、来源快照、策略版本和停止原因能否复查 |

每个维度返回 categorical status、observed metrics 和 limitations，不合成为单一真实性分数。

### 7.3 统一 gap code

搜索与研究共享稳定 code，但 severity、message 和 suggested action 由所在阶段解释：

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
| `RESEARCH_ANALYSIS_FAILED` | 研究校验阶段失败并降级；兼容响应可映射旧 `TRUST_ANALYSIS_FAILED` |

没有 locator、只有 snippet/摘要或未完成关键一致性检查时，`/research` 不能把 key finding 返回为无保留 `supported`；`/search` 则只把相应结果标为 discovery evidence。

## 8. API 与 Agent 工作流

### 8.1 用户能力面

目标稳定能力只有两个：

| 能力 | REST | MCP | 语义 |
|------|------|-----|------|
| 轻量搜索 | `POST /search` | `search` | 单轮返回首批相关 evidence 和研究种子 |
| 可信研究 | `/research` 资源族 | `research` | 创建、读取、反馈或取消多轮研究任务，交付 ResearchDossier |

`/research` 资源族包含 `POST /research`、`GET /research/{id}`、`POST /research/{id}/feedback` 和 `POST /research/{id}/cancel`。这些是同一研究任务的生命周期动作，不是额外业务能力。MCP `research` 使用 `operation=start|get|feedback|cancel` 的判别联合 schema；每个 operation 与 REST 共用同一 command model、状态机和错误码。

### 8.2 `POST /search`

Canonical 请求：

```json
{
  "query": "固态电池硫化物电解质近五年的关键路线",
  "limit": 10,
  "source_types": ["web", "academic", "patent"],
  "filters": {
    "published_from": "2021-01-01",
    "published_to": "2026-07-17",
    "languages": ["zh", "en"],
    "jurisdictions": ["CN", "US", "EP", "WO"]
  }
}
```

设计规则：

- 最小请求只有 `query`；`limit` 默认 10、范围 1–20，是最终全局结果数，不复用旧 `top_k` 的“每分支上限”语义；
- V1 不提供分页或类型配额，避免后续请求重新检索并改变 seed；需要更广覆盖时升级到 `/research`；
- `source_types` 缺省为自动路由，显式数组表示只检索这些类型；
- 过滤器必须按 source 返回 `applied/post_filtered/unsupported/not_applicable`，不能用全局 applied 暗示所有来源都执行了同一过滤；
- 排序由服务端固定为“相关性 + 来源可用性 + 基础 Evidence 质量”，只服务检索结果排序，不表示事实真实性；
- Canonical 请求不暴露 `include_pdf_text/pdf_text_mode/pdf_timeout_ms`、`trust_mode`、模型名或底层融合开关；
- 未知字段一律拒绝，REST/MCP 共用同一严格 Request model；
- 同步 deadline 应保持秒级，超时以 partial response 和 failure 表达；
- V1 只有一种稳定的轻量响应 shape，不暴露内部打分和深度诊断；需要完整研究轨迹时使用 `/research`。

Canonical 响应：

```json
{
  "schema_version": "search.v1",
  "request_id": "req_123",
  "status": "complete",
  "research_seed": {
    "search_id": "srch_123",
    "seed_snapshot_hash": "sha256:abc123",
    "created_at": "2026-07-17T10:00:00Z",
    "expires_at": "2026-07-18T10:00:00Z",
    "evidence_count": 3
  },
  "query": {
    "original": "固态电池硫化物电解质近五年的关键路线",
    "effective": "固态电池 硫化物电解质 关键技术路线",
    "filters_requested": {
      "published_from": "2021-01-01",
      "published_to": "2026-07-17",
      "languages": ["zh", "en"],
      "jurisdictions": ["CN", "US", "EP", "WO"]
    },
    "filter_execution": {
      "academic": {
        "applied": {},
        "post_filtered": {},
        "unsupported": ["published_from", "published_to", "languages"],
        "not_applicable": ["jurisdictions"]
      },
      "patent": {
        "applied": {
          "published_from": "2021-01-01",
          "published_to": "2026-07-17"
        },
        "post_filtered": {},
        "unsupported": ["languages", "jurisdictions"],
        "not_applicable": []
      },
      "web": {
        "applied": {},
        "post_filtered": {},
        "unsupported": ["published_from", "published_to", "languages"],
        "not_applicable": ["jurisdictions"]
      }
    }
  },
  "evidence": [
    {
      "id": "ev_s1",
      "type": "academic",
      "title": "Sulfide solid electrolytes: recent advances",
      "canonical_url": "https://example.org/paper/123",
      "passage": {"kind": "abstract", "text": "..."},
      "content_quality": "discovery_only",
      "provenance": {
        "publisher_name": "Example Journal",
        "retrieved_via": "openalex_local",
        "content_origin": "metadata"
      }
    },
    {
      "id": "ev_s2",
      "type": "patent",
      "title": "Sulfide electrolyte composition",
      "canonical_url": "https://example.org/patent/WO123",
      "passage": {"kind": "abstract", "text": "..."},
      "content_quality": "discovery_only",
      "provenance": {
        "publisher_name": "WIPO",
        "retrieved_via": "patent_es",
        "content_origin": "metadata"
      }
    },
    {
      "id": "ev_s3",
      "type": "web",
      "title": "固态电池技术路线综述",
      "canonical_url": "https://example.org/report/456",
      "passage": {"kind": "provider_extract", "text": "..."},
      "content_quality": "discovery_only",
      "provenance": {
        "publisher_name": "Example Institute",
        "retrieved_via": "serpapi",
        "content_origin": "provider_extract"
      }
    }
  ],
  "result_set": {
    "returned": 3,
    "limit": 10,
    "counts_by_type": {"web": 1, "academic": 1, "patent": 1}
  },
  "retrieval_assessment": {
    "status": "usable",
    "quality_mix": {
      "citable": 0,
      "limited": 0,
      "discovery_only": 3,
      "unavailable": 0
    },
    "gaps": []
  },
  "retrieval_boundary": {
    "query_time": "2026-07-17T10:00:00Z",
    "languages": ["zh", "en"],
    "jurisdictions": ["CN", "US", "EP", "WO"],
    "license_scope": ["metadata", "short_quote"],
    "candidate_limit": 60,
    "deadline_ms": 2000,
    "source_snapshot": {
      "web": "response_hash:web_abc",
      "academic": "index:2026-07-17",
      "patent": "index:2026-07-17"
    }
  },
  "failures": [],
  "meta": {
    "elapsed_ms": 820
  }
}
```

`research_seed.search_id` 对调用者是不透明、高熵研究种子；服务端只按 ID 读取不可变快照，并校验 snapshot hash 与 TTL，不接受客户端回传或修改 evidence。当前部署不建立租户身份模型，访问控制沿用 API token。只有 SeedStore 降级时 `research_seed` 才为 `null`。

### 8.3 `POST /research`

完整示例：

```json
{
  "search_id": "srch_123",
  "profile": "technology_validation",
  "depth": "standard",
  "objective": {
    "question": "固态电池硫化物电解质近五年的关键路线是什么，哪些已经形成专利布局？",
    "claims": [
      {
        "text": "硫化物电解质已经形成较完整的专利布局",
        "importance": "key"
      }
    ],
    "required_features": [
      "离子电导率", "界面稳定性", "制备方法", "产业化进展"
    ]
  },
  "scope": {
    "source_types": ["academic", "patent", "web"],
    "time": {
      "from": "2021-01-01",
      "to": "2026-07-17",
      "academic_basis": "publication_date",
      "patent_basis": "priority_date"
    },
    "languages": ["zh", "en"],
    "jurisdictions": ["CN", "US", "EP", "WO"]
  },
  "policy": {
    "id": "technical-evidence.v1"
  },
  "budget": {
    "max_rounds": 4,
    "max_candidates": 200,
    "max_deep_reads": 20,
    "deadline_ms": 120000
  },
  "privacy": {
    "mode": "restricted",
    "allow_external_processing": false
  }
}
```

成功创建返回 `202 Accepted`：

```json
{
  "schema_version": "research.v1",
  "request_id": "req_456",
  "research_id": "rsch_456",
  "state": "queued",
  "phase": null,
  "seed_search_id": "srch_123",
  "seed_snapshot_hash": "sha256:abc123",
  "evidence_set_revision": 1,
  "created_at": "2026-07-17T10:00:02Z",
  "updated_at": "2026-07-17T10:00:02Z",
  "task_revision": 1,
  "resolved": {
    "objective": {
      "question": "固态电池硫化物电解质近五年的关键路线是什么，哪些已经形成专利布局？",
      "claim_ids": ["claim_1"],
      "required_features": ["离子电导率", "界面稳定性", "制备方法", "产业化进展"]
    },
    "scope": {
      "source_types": ["academic", "patent", "web"],
      "time": {
        "from": "2021-01-01",
        "to": "2026-07-17",
        "academic_basis": "publication_date",
        "patent_basis": "priority_date"
      },
      "languages": ["zh", "en"],
      "jurisdictions": ["CN", "US", "EP", "WO"]
    },
    "seed": {"included_evidence": 3, "excluded_evidence": 0, "exclusions": []},
    "profile": "technology_validation",
    "depth": "standard",
    "policy": {"id": "technical-evidence.v1"},
    "budget": {
      "max_rounds": 4,
      "max_candidates": 200,
      "max_deep_reads": 20,
      "deadline_ms": 120000
    },
    "privacy": {"mode": "restricted", "allow_external_processing": false},
    "adjustments": []
  },
  "progress": {
    "rounds_completed": 0,
    "raw_candidates": 3,
    "independent_academic_works": 0,
    "independent_patent_families": 0,
    "documents_deep_read": 0,
    "accepted_evidence": 0,
    "remaining_gaps": null
  },
  "input_request": null,
  "dossier": null,
  "stop": null,
  "failures": [],
  "links": {
    "self": "/research/rsch_456",
    "cancel": "/research/rsch_456/cancel"
  },
  "retry_after_ms": 2000
}
```

REST 创建请求必须携带 `Idempotency-Key`，成功响应同时设置 `Location: /research/{id}` 与 `Retry-After`。MCP `operation=start` 使用必填的 `idempotency_key` 字段。服务端按 `key + canonical request hash` 全局去重：同 key、等价请求返回同一 task；同 key、不同请求返回 `409 IDEMPOTENCY_KEY_REUSED`，防止超时重试创建两份昂贵任务。

Seed 在创建时校验并复制到 ResearchTask：不存在或无权访问统一返回 `404 SEARCH_SEED_NOT_FOUND`，已过期返回 `410 SEARCH_SEED_EXPIRED`。任务一旦创建成功，不受后续 seed TTL 到期影响；来自 `partial` 执行或 `retrieval_assessment.status=unusable` 的 seed 也允许启动，规划器负责扩展并在 dossier 披露初始缺口。

### 8.4 `GET /research/{id}`

GET 支持 `detail=standard|full`，默认 `standard`；响应设置 `ETag`，客户端可用 `If-None-Match` 降低轮询流量。运行中返回稳定的 TaskEnvelope：

```json
{
  "schema_version": "research.v1",
  "request_id": "req_789",
  "research_id": "rsch_456",
  "state": "running",
  "phase": "deep_reading",
  "seed_search_id": "srch_123",
  "seed_snapshot_hash": "sha256:abc123",
  "task_revision": 7,
  "evidence_set_revision": 3,
  "created_at": "2026-07-17T10:00:02Z",
  "updated_at": "2026-07-17T10:00:18Z",
  "retry_after_ms": 2000,
  "resolved": {
    "objective": {
      "question": "固态电池硫化物电解质近五年的关键路线是什么，哪些已经形成专利布局？",
      "claim_ids": ["claim_1"],
      "required_features": ["离子电导率", "界面稳定性", "制备方法", "产业化进展"]
    },
    "scope": {
      "source_types": ["academic", "patent", "web"],
      "time": {
        "from": "2021-01-01",
        "to": "2026-07-17",
        "academic_basis": "publication_date",
        "patent_basis": "priority_date"
      },
      "languages": ["zh", "en"],
      "jurisdictions": ["CN", "US", "EP", "WO"]
    },
    "seed": {"included_evidence": 3, "excluded_evidence": 0, "exclusions": []},
    "profile": "technology_validation",
    "depth": "standard",
    "policy": {"id": "technical-evidence.v1"},
    "budget": {
      "max_rounds": 4,
      "max_candidates": 200,
      "max_deep_reads": 20,
      "deadline_ms": 120000
    },
    "privacy": {"mode": "restricted", "allow_external_processing": false},
    "adjustments": []
  },
  "progress": {
    "rounds_completed": 2,
    "raw_candidates": 86,
    "independent_academic_works": 18,
    "independent_patent_families": 11,
    "documents_deep_read": 12,
    "accepted_evidence": 8,
    "remaining_gaps": 3
  },
  "input_request": null,
  "dossier": null,
  "stop": null,
  "failures": [],
  "links": {
    "self": "/research/rsch_456",
    "cancel": "/research/rsch_456/cancel"
  }
}
```

完成或部分完成时，同一 envelope 的 `dossier` 与 `stop` 变为非空：

```json
{
  "schema_version": "research.v1",
  "request_id": "req_790",
  "research_id": "rsch_456",
  "state": "completed",
  "phase": null,
  "seed_search_id": "srch_123",
  "seed_snapshot_hash": "sha256:abc123",
  "task_revision": 12,
  "evidence_set_revision": 6,
  "created_at": "2026-07-17T10:00:02Z",
  "updated_at": "2026-07-17T10:01:47Z",
  "retry_after_ms": null,
  "resolved": {
    "objective": {
      "question": "固态电池硫化物电解质近五年的关键路线是什么，哪些已经形成专利布局？",
      "claim_ids": ["claim_1"],
      "required_features": ["离子电导率", "界面稳定性", "制备方法", "产业化进展"]
    },
    "scope": {
      "source_types": ["academic", "patent", "web"],
      "time": {
        "from": "2021-01-01",
        "to": "2026-07-17",
        "academic_basis": "publication_date",
        "patent_basis": "priority_date"
      },
      "languages": ["zh", "en"],
      "jurisdictions": ["CN", "US", "EP", "WO"]
    },
    "seed": {"included_evidence": 3, "excluded_evidence": 0, "exclusions": []},
    "profile": "technology_validation",
    "depth": "standard",
    "policy": {"id": "technical-evidence.v1"},
    "budget": {
      "max_rounds": 4,
      "max_candidates": 200,
      "max_deep_reads": 20,
      "deadline_ms": 120000
    },
    "privacy": {"mode": "restricted", "allow_external_processing": false},
    "adjustments": []
  },
  "progress": {
    "rounds_completed": 4,
    "raw_candidates": 163,
    "independent_academic_works": 42,
    "independent_patent_families": 31,
    "documents_deep_read": 20,
    "accepted_evidence": 11,
    "remaining_gaps": 1
  },
  "input_request": null,
  "dossier": {
    "assessment": {
      "status": "conflicted",
      "dimensions": {
        "coverage": {"status": "partial", "observed_claim_coverage_rate": 0.92},
        "independence": {"status": "sufficient", "independent_support_count": 3},
        "traceability": {"status": "sufficient", "observed_located_citation_rate": 1.0},
        "consistency": {"status": "mixed", "conflicted_claims": 1},
        "source_quality": {"status": "sufficient", "primary_source_count": 2},
        "reproducibility": {"status": "complete", "query_log_available": true}
      }
    },
    "findings": [
      {
        "id": "finding_1",
        "claim": "硫化物电解质已形成多条研究与专利技术路线",
        "status": "conflicted",
        "confidence": "medium",
        "qualified_support_refs": ["ev_12", "ev_31"],
        "conflict_refs": ["ev_58"],
        "limitations": ["EP 辖区部分专利全文未获取"],
        "expert_review_required": false
      }
    ],
    "evidence_index": {
      "ev_12": {
        "content_quality": "citable",
        "quote": "...",
        "locator": {
          "document_id": "doi:10.0000/example",
          "version_id": "version-of-record",
          "section": "Results",
          "page_from": 8
        },
        "provenance": {
          "publisher_name": "Example Journal",
          "content_origin": "fulltext",
          "retrieved_at": "2026-07-17T10:00:31Z"
        },
        "license": {"quote_display": "allowed"}
      },
      "ev_31": {
        "content_quality": "citable",
        "quote": "...",
        "locator": {
          "document_id": "publication:WO123",
          "version_id": "WO-A1",
          "claim_number": "1"
        },
        "provenance": {
          "publisher_name": "WIPO",
          "content_origin": "fulltext",
          "retrieved_at": "2026-07-17T10:00:49Z"
        },
        "license": {"quote_display": "allowed"}
      },
      "ev_58": {
        "content_quality": "citable",
        "quote": "...",
        "locator": {
          "document_id": "doi:10.0000/counter-example",
          "version_id": "version-of-record",
          "section": "Discussion",
          "page_from": 11
        },
        "provenance": {
          "publisher_name": "Independent Review",
          "content_origin": "fulltext",
          "retrieved_at": "2026-07-17T10:01:02Z"
        },
        "license": {"quote_display": "allowed"}
      }
    },
    "evidence_funnel": {
      "raw_candidates": 163,
      "unique_academic_works": 42,
      "unique_patent_families": 31,
      "deep_read": 20,
      "accepted_evidence": 11
    },
    "coverage": {
      "matrix": [
        {
          "dimension": "jurisdiction",
          "value": "EP",
          "status": "partial",
          "evidence_refs": ["ev_31"],
          "gap_refs": ["gap_ep_fulltext"]
        }
      ],
      "gaps": [
        {
          "id": "gap_ep_fulltext",
          "code": "CLAIM_TEXT_UNAVAILABLE",
          "severity": "warning",
          "retryable": true,
          "message": "EP 同族成员的权利要求全文未获取",
          "suggested_action": "在许可可用后重试 EP 全文读取"
        }
      ]
    },
    "boundaries": {
      "seed_boundary": {
        "source_snapshot": {
          "web": "response_hash:web_abc",
          "academic": "index:2026-07-17",
          "patent": "index:2026-07-17"
        },
        "query_time": "2026-07-17T10:00:00Z",
        "languages": ["zh", "en"],
        "jurisdictions": ["CN", "US", "EP", "WO"],
        "license_scope": ["metadata", "short_quote"],
        "candidate_limit": 60,
        "deadline_ms": 2000
      },
      "resolved_scope": {
        "time": {
          "from": "2021-01-01",
          "to": "2026-07-17",
          "academic_basis": "publication_date",
          "patent_basis": "priority_date"
        },
        "languages": ["zh", "en"],
        "jurisdictions": ["CN", "US", "EP", "WO"]
      },
      "max_rounds": 4,
      "max_candidates": 200,
      "max_deep_reads": 20,
      "deadline_ms": 120000,
      "rounds_executed": 4,
      "actual_queries": [
        {
          "round": 1,
          "query": "固态电池 硫化物电解质 关键技术路线",
          "source_types": ["academic", "patent", "web"]
        }
      ]
    },
    "artifacts": {
      "evidence_set": {
        "href": "/research/rsch_456/artifacts/evidence-set",
        "expires_at": "2026-07-24T10:01:47Z"
      }
    }
  },
  "stop": {
    "reason": "information_gain_saturated",
    "rounds_completed": 4,
    "rounds_without_new_independent_evidence": 2,
    "remaining_gap_refs": ["gap_ep_fulltext"]
  },
  "failures": [],
  "links": {
    "self": "/research/rsch_456"
  }
}
```

`standard/full` 响应中的每个 finding 引用都必须能在 `evidence_index` 解引用到 quote、locator、provenance、许可和版本。artifact 只用于导出和审计，不是核验 finding 的必要依赖；它与 task 使用相同访问控制和 privacy policy，签名地址不得成为绕过 API 鉴权的旁路。

### 8.5 反馈与取消

当 `state=needs_input` 时，TaskEnvelope 返回非空 `input_request`：

```json
{
  "id": "input_1",
  "questions": [
    {
      "question_id": "q_patent_date_basis",
      "field": "scope.time.patent_basis",
      "prompt": "专利时间边界按优先权日还是公开日？",
      "allowed_values": ["priority_date", "publication_date"]
    }
  ]
}
```

调用方通过 `POST /research/{id}/feedback` 提交：

```json
{
  "input_request_id": "input_1",
  "answers": [
    {"question_id": "q_patent_date_basis", "value": "priority_date"}
  ]
}
```

回答按 `question_id` 映射，不依赖数组顺序或可变 field path。只有当前 `needs_input` revision 接受反馈；成功返回 `202` 并转为 `queued`，未知 question、重复回答、过期 input ID 或非法状态返回 `409`。反馈追加 request revision，不覆写原请求、旧 evidence set 或审计轨迹。

`POST /research/{id}/cancel` 无请求体。`queued/running/needs_input` 可转为 `cancelled`；重复取消已取消任务返回相同结果，取消其他终态返回 `409 TASK_TERMINAL`。取消停止后续计费，但已开始的不可中断 provider 调用按实际 usage 记录。

### 8.6 状态机与停止原因

阶段可以在多轮研究中重复，不能用 phase 推导百分比。状态转移固定为：

```text
queued ──→ running ──→ completed | partial | failed
  │  └──→ failed  │  └──→ needs_input ──feedback──→ queued
  │               │                └──cancel──→ cancelled
  └──cancel───────┴───────────────────────────→ cancelled
```

| state | 是否终态 | 语义 | `stop.reason` |
|-------|----------|------|---------------|
| `queued` | 否 | 已持久化，等待 worker | `null` |
| `running` | 否 | 正在执行某个 phase | `null` |
| `needs_input` | 否 | 缺少会改变研究方向的输入，可用 feedback 恢复 | `null`，使用 `input_request` |
| `completed` | 是 | 按计划达到覆盖目标或信息增益饱和；不代表结论一定 sufficient | `coverage_target_met/information_gain_saturated` |
| `partial` | 是 | 已有可用 dossier，但被轮次、deadline、预算或来源故障提前终止 | `max_rounds_reached/deadline_reached/budget_exhausted/source_unavailable` |
| `failed` | 是 | 未形成可用 dossier且不可恢复 | `source_unavailable/unrecoverable_error` |
| `cancelled` | 是 | 用户取消 | `cancelled` |

每个终态都返回 `stop` 和 `remaining_gap_refs`；`failed` 无 coverage 时直接返回结构化 failures。`needs_input` 不是停止原因。“未找到”不得改写为“不存在”，`completed` 与 dossier 的 `assessment.status` 相互独立。

### 8.7 MCP `research` 契约

| operation | 最小输入 | 输出 |
|-----------|----------|------|
| `start` | `search_id`, `idempotency_key` | 与 REST `202` body 同构的 TaskEnvelope |
| `get` | `research_id`, 可选 `detail` | 与 REST GET 同构；任务不依赖 MCP 会话存活 |
| `feedback` | `research_id`, `input_request_id`, `answers` | 新 task revision 与 `queued` 状态 |
| `cancel` | `research_id` | 幂等取消后的 TaskEnvelope |

MCP transport error 只表示工具调用本身未送达；已接受任务的业务 failure 必须通过 TaskEnvelope 返回。MCP 与 REST 使用相同 error code、API 身份、retention 和 artifact 权限。

### 8.8 标准 Agent 工作流

```text
search(query)
  → 直接回答低风险、发现型问题；或取得 research_seed.search_id
  → research.start(research_seed.search_id, profile/depth/objective)
  → research.get(research_id)
  → 若 needs_input，则提交 feedback 后继续 get
  → 直到 completed/partial/failed/cancelled
  → 只从 findings + qualified evidence 组织结论
  → 同时披露 coverage gaps、boundaries 和 stop reason
```

Agent 不直接调用 PDF 网关或自行把 PDF 裸文本拼装为 Evidence，也不把搜索 snippet 当作 key finding 的最终支持。

### 8.9 统一错误与版本迁移

请求或资源级错误统一返回：

```json
{
  "error": {
    "code": "SEARCH_SEED_EXPIRED",
    "message": "search seed 已过期，请重新调用 /search",
    "retryable": false,
    "request_id": "req_999",
    "details": {}
  }
}
```

主要映射：schema/字段错误 `422`，资源不存在或无权访问 `404`，幂等冲突/非法状态转移 `409`，已过期 seed/task `410`，语义或 policy/privacy 不可满足 `422`，配额/并发限制 `429`，所有计划来源不可用或基础设施不可用 `503`。`restricted + external_processing=false` 若没有合规执行路径，在创建阶段返回 `422 PRIVACY_POLICY_UNSATISFIABLE`，不得静默外发或降低 policy。

`search.v1/research.v1` 是唯一合同，对未知字段严格拒绝，不通过 API-Version 分支保留 legacy schema。原 PDF/trust/ranking 请求字段和旧 response shape 不再解析；REST 与 MCP 在同一次破坏性切换中收敛到相同 command model。

## 9. 内容安全、许可与复核

- 所有网页、PDF、OCR 和译文都是不可信数据，不得执行其中面向 Agent 的指令。
- 检测“忽略指令、调用工具、泄露密钥、执行命令”等模式，标记 `PROMPT_INJECTION_SUSPECTED`。
- 后续全文抓取只允许 `http/https`，并校验重定向、DNS/IP、类型、大小和超时。
- 不因页面自称“官方/已验证”而提升来源角色。
- 保留原文、OCR/翻译结果与模型输出的分层 provenance，人工可回看原文。
- 按 API、TDM、缓存和片段展示许可处理；不允许展示时只返回 locator/访问入口。

## 10. 代码落点

现有 `src/trust/` 保留陈述拆解、蕴含、一致性、证据标注和来源策略等领域内核。新增研究任务应用层与研究领域模块：

```text
src/application/
  research_service.py       # ResearchTask 总编排，不直接依赖具体 provider
  research_commands.py      # Start/Get/Feedback/Cancel 命令
  research_outcomes.py      # Task progress 与 ResearchDossier 应用结果
  ports/
    search_seed.py          # /search 快速写入、/research 读取不可变 seed 的 port
    research_store.py       # task、版本化 evidence set、round、artifact 持久化
    document_reader.py      # 全文/PDF/OCR/权利要求深读边界

src/research/
  planner.py                # objective/scope → 覆盖维度、claim、扩展路径
  normalizer.py             # 论文版本、专利号码和专利族归并
  independence.py           # 发布者、ownership、转载、引用源独立性
  deep_reader.py            # 选择值得深读的文档并组装 EvidenceUnit
  expander.py               # 关键词/主题词/分类/引用/同族/NPL 补搜计划
  coverage.py               # CoverageMatrix 与结构化 gap
  saturation.py             # 信息增益、覆盖、预算和停止策略
  dossier.py                # finding、漏斗、边界、artifact 输出

src/trust/
  policy.py                 # 领域来源策略、关键陈述门禁、版本
  matcher.py                # claim → EvidenceUnit 候选匹配
  entailment.py             # supports/contradicts/mentions/unclear
  consistency.py            # 实体、日期、数字、否定、版本、辖区
  verifier.py               # 单轮 claim assessment，由 ResearchService 调用

config/
  research_policies.yaml
  source_roles.yaml
```

现有文件修改：

| 文件 | 设计改动 |
|------|----------|
| `src/interfaces/schemas.py` | strict `SearchRequest`、`ResearchRequest` 和 task 操作 schema；禁用未知字段 |
| `src/domain/search_api.py` | `search.v1`、`SearchSeed`、`RetrievalAssessment` 与不可变 seed snapshot |
| `src/domain/research.py` | `research.v1` TaskEnvelope、resolved scope、coverage、finding 与 dossier |
| `src/application/search_service.py` | 保持单轮轻量检索；生成不可变 search seed，不同步执行 PDF 深读或 claim verification |
| `src/application/ports/retrieval.py` | 向 research 公开 actual query/filter/limit/snapshot 诊断，不改变 provider 接口职责 |
| `src/infrastructure/openalex_pdf.py` | 作为 DocumentReader adapter；续读必须生成标准 EvidenceUnit，保证 chunk 连续 |
| `src/engine.py` | 只暴露 search 与 ResearchTask 生命周期门面 |
| `src/api.py` | `/search` 和 `/research` 资源族；旧公开路由已删除 |
| `src/mcp_server.py` | 工具只保留 `search/research` |
| `src/bootstrap.py` | 装配 ResearchService、ResearchStore、DocumentReader、policy registry 与 task worker |
| `src/pipeline/rerank.py` | 保持相关性职责，不混入 claim support 或研究可信维度 |

SearchSeedStore 是 `/search` 的轻量基础依赖，与研究 worker/模型隔离；ResearchStore 保存 task state、已复制的固定 seed、版本化 evidence set、执行轮次和来源快照。MVP 使用 SQLite/WAL 与独立 research executor，并在启动时恢复 `queued/running` 任务；生产多实例应替换为共享持久化和分布式 worker，不得依赖进程内内存保证研究任务可恢复。

模块二的全文/OCR/专利权利要求解析仍是强可信能力的前置依赖。所有 reader 输出必须对齐 EvidenceUnit，禁止把裸 text 直接交给 verifier 或报告生成器。

## 11. 失败、预算与降级

### 11.1 `/search` 的失败语义

`/search` 的首要目标是可预测地快速返回，不为追求完整性自动转成长任务：

- 请求格式、认证、配额或权限错误分别使用标准 `4xx`；调用方可以立即修正，不创建 search seed。
- 至少一个检索分支完成时返回 `200`，并以顶层 `status=complete/partial` 区分完整执行和部分来源/过滤失败；成功执行但无结果使用 `complete + retrieval_assessment.status=unusable + NO_EVIDENCE`。
- Provider 超时、过滤不支持和正文不可用写入结构化 `failures[]/gaps[]`，包含 `code/stage/source/retryable/message`；不得只写日志。
- 所有计划来源都不可用时返回 `503`；若已有可用 evidence，则返回 `200 partial`，不因单源故障丢弃结果。
- 搜索执行完成后，即使结果为空或部分失败，也固定保存实际 query、边界和 evidence 快照并签发 `search_id`，使 `/research` 可以在相同边界上补搜；认证/校验失败不签发。
- SearchSeedStore 临时不可用时仍返回已取得的 evidence，以 `200 partial + research_seed=null + RESEARCH_SEED_UNAVAILABLE` 明确降级；该响应不能直接启动 `/research`，调用方可稍后重试 `/search`。研究 worker、全文或模型故障不影响 seed 写入。
- `/search` 不在超时后后台继续深读，也不因 source failure 隐式创建 `/research` 任务。

### 11.2 `/research` 的失败语义

`POST /research` 一旦返回 `202`，后续业务失败都体现在任务资源中，轮询同一 task 不因某个阶段失败随机切换为 HTTP `5xx`：

- `completed`：任务按计划达到覆盖目标或信息增益饱和，返回 dossier；dossier 自身仍可是 `insufficient/conflicted`，也可有非关键 failure。
- `partial`：达到轮次、deadline、预算或来源边界而提前停止，已有可用 finding/dossier，但计划未正常完成。
- `needs_input`：目标、关键日期口径或权限缺失会实质改变研究方向；返回结构化问题和可接受字段，不把普通缺证升级为向用户提问。
- `failed`：任务在形成任何可用 dossier 前遇到不可恢复错误；保留阶段、已耗预算、可重试性和诊断，不删除执行轨迹。
- `cancelled`：停止新工作并保留取消前可合法返回的部分结果；取消操作必须幂等。

阶段降级遵循保守原则：

- provenance/locator 解析失败时保留发现线索并标为 `discovery_only`，不伪造 locator；
- entailment 模型超时后可降级到字面和结构化规则，规则不能直接证明时返回 `insufficient`；
- 反证检索失败、OCR/翻译失败或全文受限时保留现有 evidence，并写入 gap、failure 和 stop reason；
- 单一研究阶段失败不得回写或改变原 `/search` 响应，只影响 ResearchTask；
- source 恢复后重试必须从最后一个已提交的 `evidence_set_revision` 继续，避免重复计数和重复计费。

### 11.3 预算、缓存与隔离

- `depth` 是普通调用者的成本预设；显式 budget 只能收紧服务端上限，不能绕过全局资源上限、许可、来源策略或人工复核策略。响应返回 `resolved.budget` 与实际 usage。
- 深挖由覆盖目标、`max_rounds/max_candidates/max_deep_reads/deadline_ms`、连续轮次新增独立 evidence 数和单位预算信息增益共同停止。
- 缓存键至少包含 `objective/scope + seed/evidence version hashes + source snapshot + policy/model/parser version`；时效 claim 不跨时间截点复用。
- `search_id/research_id` 使用不可预测的高熵 ID，并由统一 API 鉴权保护；日志与 artifact 遵循 privacy mode，restricted 原文不得进入未授权外部模型或共享缓存。
- 小模型负责陈述拆解、字段抽取和候选定位；大模型只处理复杂蕴含、冲突分析与综合，按复杂度路由并记录实际模型版本。
- 服务端限制全局并发研究数与排队时长；预计无法在任务 TTL/预算内启动时应在创建阶段明确拒绝，而不是无限期保持 `queued`。

## 12. 评测设计

### 12.1 统一口径

评测固定候选源与索引快照、查询时间、语言/辖区、日期口径、许可、policy、模型/解析器版本和预算。`/search` 与 `/research` 分开评测：前者以轻量召回、契约稳定性和尾延迟为主，后者以独立有效证据、原文校验、覆盖披露和任务可靠性为主，不能用 retrieval NDCG 代替研究可信度。

首批基准计划包含 50 条研究任务：学术 20、专利 20、跨源技术情报 10，并为每条任务标注必要特征、独立研究成果/专利族、关键原文 locator、支持/冲突关系和允许的覆盖边界。文档中的数值只可标为产品 target 或本次 observed result；未完成人审前不得写成已达到的效果。

### 12.2 `/search` 指标

| 指标 | 定义 |
|------|------|
| 轻量召回质量 | 固定快照下的 Recall@K、NDCG 与来源类型覆盖，作为发现能力护栏 |
| 过滤执行准确率 | requested/applied/unsupported 与 provider 实际执行是否一致 |
| 结果身份准确率 | canonical document、来源类型和基础 provenance 是否正确 |
| 种子可复现率 | 使用 `search_id` 能否读取与原响应相同的 evidence、hash 和边界 |
| 部分失败可见率 | 单源超时、降级和不支持过滤是否完整进入 response |
| 接口轻量性 | p50/p95 延迟、响应体大小、一次请求成功率和超时率 |

### 12.3 `/research` 指标

| 指标 | 定义 |
|------|------|
| 独立有效证据召回率 | 标注集合中的独立且合格 evidence 被发现并采纳的比例 |
| 论文版本归并准确率 | 预印本、正式版、勘误和撤稿是否归到正确研究成果并保留版本关系 |
| 专利族归并准确率 | 同族成员是否正确归并，优先权与辖区差异是否保留 |
| 陈述支持精确率 | 判为 `supported` 的陈述中，人工确认被原文在限定条件下直接支持的比例 |
| Claim 覆盖率 | 必要 claim/技术特征中至少有一条合格 evidence relation 的比例；缺失项另计 gap disclosure |
| Locator 准确率 | locator 能否稳定回到正确版本和原文单元 |
| 冲突/反证发现率 | 标注有冲突或有效反例的 claim 中系统找到并正确关联的比例 |
| 无依据陈述率 | dossier 中无合格支持且未标为 inference/gap 的事实句比例 |
| 缺口披露率 | 未覆盖维度、失败路径、未执行反证和预算停止是否显式披露 |
| 可复现率 | 固定快照和版本下，执行轨迹能否复建相同 evidence set 与 finding 状态 |
| 饱和停止效率 | 达到相同有效覆盖所需轮数、深读数、时延和成本，以及停止后的遗漏量 |
| 任务可靠性 | accepted task 到合法终态比例、恢复成功率、幂等重复任务率和取消生效率 |

所有质量指标同时报告时延、模型/来源成本、轮数、候选数、深读数、降级率和人工复核量，防止通过无限扩大预算换取表面覆盖。

### 12.4 必测边界样本

1. 三个 provider 返回同一页面，或不同域名转载同一稿件。
2. 同一论文存在预印本、正式发表、勘误与撤稿；摘要支持但正文限定不支持。
3. 多个辖区的专利同族成员摘要相似，但权利要求范围和法律状态不同。
4. 申请日、优先权日、公开日、发表日与研究要求的截止日混淆。
5. 官网规格与独立测试冲突，或多个网站都引用同一上游来源。
6. 新旧法规、不同辖区、数字单位、否定和推断强度被混用。
7. OCR/翻译错误、提示注入、全文无许可和仅 locator 可返回。
8. `/search` 空结果、单源超时、不支持过滤和 search seed 过期。
9. 不可预测 ID、重复 Idempotency-Key、worker 重启、阶段重试与取消竞争。
10. 达到 max rounds 但关键 gap 未解决，以及连续轮次无新增独立 evidence。

### 12.5 MVP 发布门槛

- `/search` 默认路径不触发 PDF 深读、claim 校验或研究 worker，且达到单独设定的轻量延迟与响应体 target。
- `/search` 的 filter、status、failure、`research_seed` 和 seed snapshot 行为通过契约测试；单源失败只产生 `partial`。
- `POST /research` 具备全局幂等、持久化状态机、固定 seed 复制、重启恢复、预算上限和可取消性；已接受任务不会因进程重启或 seed TTL 到期静默丢失。
- 没有稳定 locator 的 evidence 不得单独产生高置信 `supported`，snippet/摘要不得替代权利要求、方法、结果或法规原文。
- 检测到有效冲突时不得输出无保留 `supported`；同一研究成果、canonical document、转载链或专利族不得虚增为多个独立来源。
- 反证未检索、版本未消歧、来源失败、未覆盖 scope 和预算停止必须在 dossier 中显式披露；“未找到”不得解释为“不存在”。
- 研究阶段失败不改变或拖慢已经完成的 `/search`；`partial/failed/needs_input` 都有稳定、可机器处理的语义。
- 所有 query、过滤、来源快照、规则、policy、模型、解析器、evidence set 和 artifact 带版本并可审计。

## 13. 分阶段实施

### Phase 0：可信 Evidence 与陈述校验内核（已实现）

- 已有 Web/Academic/Patent Evidence 标注、基础 provenance/locator/content quality，以及 claim 拆解、候选匹配、蕴含和一致性检查。
- 原 verify/PDF 能力已经成为研究内部构件，公开路由与 MCP 工具已删除。
- 本阶段内核仍受摘要、provider extract 和初步 locator 限制；主动补搜由 ResearchService 的首版多轮闭环承担。

### Phase 1：接口收敛与任务骨架（已实现）

- 发布 strict `search.v1`：保留单轮轻量检索，生成不可变 `search_id`，返回 `retrieval_assessment` 与真实 filters/failures。
- 实现 SearchSeedStore、ResearchStore、SQLite/WAL 持久化、独立 worker、ResearchTask 状态机、幂等创建、查询、反馈、取消和预算上限。
- 已发布完整 `/research` 资源族与 MCP `research`；首版编排内部 verify/PDF 能力，并输出 dossier、coverage gap、执行边界与停止原因。
- REST/MCP 已直接切换到新契约，未保留 legacy schema 或旧入口。

退出条件：轻量搜索契约、seed 可复现、task 重启恢复和接口隔离通过测试；research 故障不会增加 `/search` 尾延迟。

### Phase 2：多轮研究闭环增强（进行中）

- 全文 reader 统一生成带稳定 locator 的 EvidenceUnit；学术定位到章节/段落/图表，专利定位到权利要求/说明书段落。
- 完成论文版本、专利号码/专利族、转载链与发布者 ownership 归并，正确计算独立 evidence。
- 加入关键词、主题词、分类号、引用/被引、同族、NPL、主体和发明人等扩展路径，以及 key claim 主动反证。
- 用 CoverageMatrix 的缺口驱动下一轮，以独立有效 evidence 的边际信息增益、覆盖目标和预算共同停止。

退出条件：关键 finding 通过原文门禁；版本/专利族归并、反证发现和停止策略达到预先登记的评测 target。

### Phase 3：研究档案、反馈与校准

- 交付文献地图、专利全景、证据图谱、查询审计和可导出的 ResearchDossier artifact。
- 支持 `needs_input` 的结构化反馈、人工复核结果回写和 policy 版本迁移，但不得重写已完成的 evidence set；新反馈创建新版本。
- 建立离线基准、线上任务漏斗、来源漂移、成本/延迟和人工复核监控，按 profile 校准 categorical confidence。
- 持续校准 categorical confidence，并完善 artifact 保留和审计策略。

## 14. 待确认事项

以下决策不改变两个接口的能力边界；Phase 1 已采用表中默认值，生产化前仍需继续固化保留、许可与审计策略：

| 决策 | 建议默认值 | 影响 |
|------|------------|------|
| SearchSeed 存储与 TTL | 共享持久化、默认 24 小时；TTL 写入响应 | 决定跨进程复现、存储成本和过期重搜体验 |
| Task/Artifact 保留期 | task 元数据长于原文 artifact；部署级配置删除策略 | 兼顾审计、许可、隐私和成本 |
| 任务事件通道 | MVP 轮询；保留 SSE/event hook，不把流式传输作为第三种能力 | 影响长任务 UX 与网关复杂度 |
| `needs_input` 反馈方式 | REST 使用 `/research/{id}/feedback` 子资源；MCP 使用 `operation=feedback` | 保证反馈有审计记录且不覆盖原请求 |
| 取消方式 | REST 使用幂等 `/research/{id}/cancel` 动作；MCP 使用 `operation=cancel` | 区分停止计算与删除历史 |
| 首批稳定 locator | 按目标 profile 决定：literature 先 Academic，prior-art 先 Patent | 决定 Phase 2 的上线顺序与可支持场景 |
| Source snapshot | provider 有版本则记录版本；否则记录实际 query/time/response hash 并声明限制 | 决定可复现性维度能否标为 complete |
| Policy 与人工复核 | 服务端 registry 管理；高风险领域、关键冲突和法律判断强制复核 | 防止客户端降低可信门槛 |
| 全文与模型许可 | 统一 license registry；restricted 默认不外发原文 | 决定缓存、展示、OCR/翻译和模型路由 |

## 15. 非目标

- 不把 domain reputation、被引数、结果数量或模型自信包装成真实性概率。
- 不用一个固定线性 `relevance + trust` 总分替代 claim、来源独立性和覆盖评估。
- 不承诺穷尽全网、所有论文、所有专利族或全部冲突证据，也不把“未找到”写成“不存在”。
- 不在缺少原文与 locator 时宣称权利要求、实验方法、法规条文或关键 finding 已获强支持。
- 不隐藏小众来源、反例、冲突、失败路径、许可限制或预算停止原因。
- 不由 Research Layer 裁决法律有效性、侵权/FTO/可专利性、科学真理、医疗判断或商业决策。
- 不让 `/search` 因研究逻辑变成高延迟、不可预测或需要理解内部模型参数的接口。
