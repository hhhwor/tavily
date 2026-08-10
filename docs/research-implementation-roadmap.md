# Research 功能落地技术路线

> 状态：M0–M3 单机基线已实施，M4 待推进
>
> 基线：当前 `search.v1 / research.v1` 工作树
>
> 日期：2026-08-04
>
> 关联文档：[可信研究接口设计](./agent-search-trust-layer-design.md) · [架构解耦设计](./architecture-decoupling-design.md) · [当前技术路线](./tech-route-summary.md)

## 1. 结论

Research 不需要另建一套搜索系统，也不建议立即拆成微服务。正确路线是在现有模块化单体上继续演进：

1. 保留 `search → immutable seed → async research` 的两层接口。
2. 保留 `DiscoveryService`、三领域 Ranking、统一 Evidence 和 ClaimVerifier。
3. 先兑现 privacy、policy、scope、deadline 和 budget 的执行语义。
4. 再把固定查询列表改造成由 Coverage Gap 驱动的研究循环。
5. 补齐 Web、Academic、Patent 三类原文深读和稳定 locator。
6. 最后再建设分布式任务、租户治理和生产级可观测性。

当前实现可视为“单机异步证据验证 MVP”，不能直接宣称已经具备穷尽式 Deep Research。近期目标应是交付一个边界明确、可恢复、可核验的 Research Dossier，而不是追求更长的自动生成报告。

## 2. 当前基线

已经具备的基础：

- `POST /search` 生成不可变 SearchSeed，并保存 evidence、query、filters 和 retrieval boundary。
- `POST /research` 支持幂等创建，`GET` 支持轮询与 ETag，另有 feedback/cancel。
- ResearchTask 与固定 seed 使用 SQLite/WAL 持久化。
- 独立 ResearchDispatcher 在应用启动时恢复 queued/running 任务。
- Research 复用 Discovery、EvidenceAssembler、TrustAnnotator、PDF Gateway 和 VerifyService。
- Evidence 已区分 provenance、locator、quality、citation、patent metadata。
- ClaimVerifier 已支持支持/冲突/提及/不明确关系、逐字引文校验和独立来源门禁。
- Research 已按 Coordinator/Runner/Planner/Coverage 拆分，并按 gap 保存逐轮 checkpoint。
- Academic/Web/Patent 原文 reader、版本身份归并和可解引用 locator 已接入。
- 结构化 summary/finding/conflict/limitation/methods、引用审计和四类 artifact 已接入。
- 可选外部 synthesis 受 privacy router、deadline、单 operation 幂等和确定性回退约束。
- 当前生命周期由 [research_service.py](../src/application/research_service.py) 作为兼容门面协调。
- 公开任务合同位于 [domain/research.py](../src/domain/research.py)。
- SQLite 任务实现位于 [sqlite_research_store.py](../src/infrastructure/sqlite_research_store.py)。
- 运行时装配与恢复入口位于 [bootstrap.py](../src/bootstrap.py)。

当前落地状态与剩余边界：

| 范围 | M0–M3 当前行为 | M4/后续边界 |
|---|---|---|
| Privacy | restricted 在 rewrite/rerank/verify/synthesis/cache 边界执行本地或 disabled 路由 | tenant egress policy、对象级授权和访问审计 |
| Policy | policy 驱动来源要求、证据门禁、反证、独立性和停止条件 | policy 灰度、租户覆盖和版本退役治理 |
| Scope | seed 与所有新增 Evidence 均执行统一 post-retrieval gate | 更多 time basis/分类体系需先补行为实现再开放 |
| Planning | 原子 claim、Coverage Gap 和历史 gain 驱动下一轮 action | 分布式 planner 调度和人工审批节点 |
| Deep read | Academic/Web/Patent 统一 reader，支持续读、版本、许可与稳定 locator | OCR 服务治理、更大文档的对象存储和异步解析 |
| Identity | 论文 work/version、Web 转载/ownership、Patent family/version 分离归并 | 跨租户 identity registry 和人工 merge/split |
| Progress | 阶段可观察，每轮 plan/action/evidence/gain/usage 事务 checkpoint | 跨实例 event stream、trace 和 stuck-task 告警 |
| Durability | SQLite/WAL、有界队列、启动恢复、synthesis/artifact 幂等 | 共享数据库、durable queue、lease/heartbeat 和重复投递治理 |
| Output | 结构化结论、冲突、限制、方法、引用审计和四类 artifact | 对象级授权、后台物理删除、备份和跨实例 artifact storage |

## 3. 功能边界

Research 负责 claim 拆解、gap 驱动检索、原文深读、身份归并、主动反证、覆盖评估和 Dossier；不承诺穷尽检索，不把 relevance/来源数量包装成真实性概率，也不替代法律、医疗、金融或专利判断。摘要、snippet 和 provider extract 只能用于发现，不能单独证明关键 claim。

## 4. 目标架构

```text
REST / MCP
    │
    ▼
ResearchTaskCoordinator
    ├── Start / Get / Feedback / Cancel
    ├── Idempotency / Revision / Ownership
    └── Queue admission
    │
    ▼
ResearchRunner
    ├── PolicyResolver + ExecutionContext
    ├── ResearchPlanner
    ├── DiscoveryService（复用）
    ├── DeepReadService
    ├── IdentityResolver
    ├── VerifyService（复用并增强 deadline）
    ├── CoverageEvaluator
    └── SynthesisService
    │
    ▼
ResearchStore
    ├── Task / Attempt / Lease
    ├── Round / Event / Checkpoint
    ├── EvidenceSet revision
    └── Artifact metadata
```

关键原则：

- `ResearchTaskCoordinator` 只负责用户操作和状态转移。
- `ResearchRunner` 只负责一个 attempt 的阶段执行。
- `ResearchPlanner` 不做 I/O，只从目标、gap 和历史生成 action。
- `DeepReadService` 不参与排序，只把选中的文档变成可定位 Evidence。
- `CoverageEvaluator` 不生成答案，只判断覆盖和下一步缺口。
- `SynthesisService` 只消费 qualified findings，不直接使用未校验候选。

## 5. 建议代码落点

不进行一次性目录搬迁。先保留公开模型和导入路径，新增内部组件：

```text
src/application/research/
  coordinator.py          # Start/Get/Feedback/Cancel 与状态转移
  runner.py               # 单次 attempt 的阶段循环
  planner.py              # ObjectivePlan、Gap → Action
  policy.py               # Policy registry 与 resolved policy
  coverage.py             # CoverageMatrix 和信息增益
  identity.py             # work/family/syndication/ownership 归并
  deep_read.py            # reader 选择与预算分配
  synthesis.py            # Dossier summary 和 artifact 输入

src/application/ports/
  deep_reader.py          # Web/PDF/Patent 原文读取 Port
  research_queue.py       # enqueue/claim/renew/ack/reject
  model_router.py         # privacy-aware 模型选择
  artifact_store.py       # 报告与大体积 evidence 导出

src/domain/
  research.py             # 保留公开 research.v1 envelope
  research_plan.py        # 内部 ObjectivePlan/ResearchAction/RoundResult
  research_policy.py      # Policy 与 ExecutionDecision
  research_events.py      # 事件与 checkpoint 合同

src/infrastructure/readers/
  academic_pdf.py         # 复用并增强 OpenAlex PDF
  web_document.py         # 安全网页读取与正文解析
  patent_document.py      # claims/specification/段落读取
```

原 `ResearchService` 先变成兼容门面，委托 Coordinator；待调用方迁移完成后再删除大类中的旧私有方法。

## 6. 核心执行模型

### 6.1 ExecutionContext

所有外部调用必须接收同一个不可变执行上下文：

```python
class ExecutionContext:
    research_id: str
    attempt: int
    policy: ResolvedPolicy
    privacy: ResolvedPrivacy
    deadline: Deadline
    cancellation: CancellationToken
    budget: BudgetLedger
    principal_id: str
```

`BudgetLedger` 至少记录：

- provider raw candidates
- adopted candidates
- deep-read documents、pages 和 bytes
- model requests、input/output tokens
- elapsed time
- retry count
- estimated/actual external cost

所有 Retrieval、Ranking、Reader、Rewrite、Entailment 和 Synthesis 调用前统一执行：

1. cancellation 检查；
2. deadline 剩余量检查；
3. privacy/egress 决策；
4. 对应预算预留；
5. 调用；
6. 使用量提交或释放；
7. 结构化 event 记录。

### 6.2 Policy Registry

不要继续使用服务内字典拼 profile。每个 policy 是服务端版本化配置：

```python
class ResearchPolicy:
    id: str
    version: str
    allowed_profiles: set[str]
    required_source_types: set[DocumentKind]
    key_claim_gate: ClaimGate
    counterevidence_required: bool
    accepted_evidence_origins: set[str]
    coverage_dimensions: list[str]
    saturation_rounds: int
    model_route: str
```

建议的默认门禁：

| Profile | 关键门禁 |
|---|---|
| technology_landscape | 广度、来源类型和实体/主题聚类；允许 discovery evidence，但必须标级 |
| literature_review | 关键 finding 必须有 PDF/HTML 原文 locator；归并 preprint/version/correction |
| technology_validation | 一手来源或两个独立 citable 来源；必须执行反证 |
| prior_art_landscape | 专利 claims/说明书定位、优先权/公开日口径、同族与 NPL；不输出法律结论 |

客户端可以请求更严格的 policy，不能把服务端门禁降级。

### 6.3 Research Plan

Planner 输出可持久化的计划，而不是直接输出 query 字符串：

```python
class ObjectivePlan:
    question: str
    claims: list[CandidateClaim]
    coverage_targets: list[CoverageTarget]
    ambiguities: list[InputQuestion]

class ResearchAction:
    id: str
    round: int
    kind: Literal[
        "search", "counter_search", "deep_read",
        "citation_expand", "family_expand", "entity_expand"
    ]
    target_gap_refs: list[str]
    source_types: list[DocumentKind]
    query: str | None
    candidate_ids: list[str]
    expected_gain: list[str]
```

当日期口径、辖区或必要技术特征会改变检索方向时，Planner 返回结构化 `InputQuestion`，Coordinator 将任务转为 `needs_input`。

### 6.4 研究循环

```python
plan = planner.build(objective, scope, policy, seed)
evidence_set = identity.normalize(seed.evidence)

while context.budget.can_start_round():
    assessments = verifier.verify(plan.claims, evidence_set, context)
    coverage = coverage_evaluator.evaluate(plan, evidence_set, assessments)

    if coverage.target_met:
        stop("objective_satisfied")
        break

    actions = planner.next_actions(plan, coverage.gaps, history, context.budget)
    if not actions:
        stop("information_gain_saturated")
        break

    search_actions, read_actions = planner.partition(actions)
    previous = evidence_set
    discoveries = discovery.execute(search_actions, context)
    scoped = policy_gate.filter(discoveries, scope, policy)
    normalized = identity.merge(evidence_set, scoped)
    enriched = deep_read.execute(read_actions, normalized, context)
    evidence_set = identity.merge(normalized, enriched)

    gain = coverage_evaluator.measure_gain(previous, evidence_set)
    checkpoint(round, actions, evidence_set, gain, context.usage)

    if gain.is_saturated(policy.saturation_rounds):
        stop("information_gain_saturated")
        break
```

不能再使用“一轮没有新增 URL 就停止”。建议饱和条件为连续两轮同时满足：

- 没有新增独立且合格的 Evidence；
- 没有 coverage item 从 missing → partial/covered；
- 没有新增有效冲突或反证；
- 没有更稳定的 locator 或更高版本质量。

## 7. 深读路线

| 顺序 | Reader | 实施重点 | 上线门禁 |
|---|---|---|---|
| 1 | Academic | 复用 `OpenAlexPdfGateway`；cursor 续读；保存 content hash、parser version、page/chunk；归并 preprint/正式版/勘误/撤稿 | 关键 finding 可回到文档版本和页面 |
| 2 | Web | 安全抓取原页；canonical URL、ETag/Last-Modified、content hash；转载/ownership 归并；license/robots/TDM | SSRF、重定向、MIME、大小、压缩比和总时长均有硬限制 |
| 3 | Patent | claims/description/paragraph；priority/application/publication date；family、CPC/IPC、citation/NPL | claim number/paragraph 可定位；同族差异被保留 |

Web Reader 只允许 http/https，每次重定向重新校验 DNS/IP，并拒绝 loopback、link-local、metadata service 和私网地址。原文不可缓存或展示时，只保存 locator 和许可允许的短引文。专利摘要只用于发现，不能单独支持 novelty、claim scope 或法律状态结论。

## 8. Evidence 身份与采纳

统一区分 `DocumentVersionId`（具体可定位版本）与 `IndependentWorkId`（研究成果、专利族或同源稿件）。Academic 使用 DOI/version/content hash → work cluster；Patent 使用 publication/version → family + priority root；Web 使用 canonical URL/content hash → syndication/ownership cluster。

Evidence 只有通过 scope/许可门禁、身份归一化、原文回查、locator/version 一致性、quality 重算和 claim consistency 后才能进入 adopted set。

## 9. 状态、事件与持久化

### 9.1 状态机

保留现有公开状态：

```text
queued → running → completed | partial | failed
            └────→ needs_input → queued
queued/running/needs_input ───→ cancelled
```

phase 只表示当前工作，不表示百分比：

`planning → expanding → deep_reading → normalizing → verifying → coverage_analysis → synthesizing`

每次 phase 变化和每轮完成都递增 `task_revision`；只有 evidence set 原子提交时递增 `evidence_set_revision`。

### 9.2 存储演进

SQLite MVP 增加 migration，并拆分 `research_tasks`、`research_attempts`、`research_rounds`、`research_events`、`research_evidence_sets`、`research_artifacts`；大体积全文不再写入 task 单行 JSON，同时配置 `busy_timeout`、WAL checkpoint 和专用状态目录。

进入多进程/多实例、出现明显队列积压、单任务体积失控或需要租户审计时，迁移共享数据库和 durable queue。生产队列提供 `claim/lease/renew/ack`；昂贵外部操作使用稳定 operation id，保证 at-least-once 下的业务幂等。

## 10. 接口演进

继续保留当前五个 REST 资源和一个 MCP lifecycle tool：

- `POST /search`
- `POST /research`
- `GET /research/{id}`
- `POST /research/{id}/feedback`
- `POST /research/{id}/cancel`
- MCP `research(operation=start|get|feedback|cancel)`

`research.v1` 内优先增加可选字段，避免并行维护两套状态机：

- `resolved.policy_version`
- `resolved.execution_route`
- `progress.current_round / last_checkpoint_at`
- `usage`
- `dossier.summary`
- `dossier.rounds`
- typed `artifacts`
- `input_request.id` 和 typed questions

若必须删除或改变已有字段语义，再一次性发布 `research.v2`；不要通过请求参数维持 v1/v2 两条执行分支。

API 语义要求：

- 不能满足 privacy/policy/scope 时，在创建任务前返回 422。
- 队列已满或预计无法按时启动时返回 429，并带 `Retry-After`。
- 任务已接受后的业务失败进入 TaskEnvelope，不随机转成 HTTP 500。
- `completed` 表示合法停止，不表示 assessment 一定 sufficient。
- `partial` 表示已有可用 dossier，但受预算、deadline 或来源故障提前终止。

## 11. 分阶段实施

当前实施状态：

| 阶段 | 状态 | 已交付能力 |
|---|---|---|
| M0 | 已完成 | 执行上下文、隐私路由、策略与 scope 门禁、deadline、预算、有界队列和资源隔离 |
| M1 | 已完成 | Coordinator/Runner/Planner/Coverage 拆分、gap 驱动循环、逐轮 checkpoint、needs_input/feedback |
| M2 | 已完成 | Academic/Web/Patent 原文读取、稳定版本与 locator、证据采纳门禁、身份归并 |
| M3 | 已完成 | 结构化 Dossier、受控 synthesis、引用审计、四类 artifact、50 条质量语料与门禁 |
| M4 | 未开始 | 多租户、多实例、对象级授权、物理删除和生产可观测性 |

### M0：执行语义加固

目标：先保证已公开参数不会说一套、执行另一套。

工作项：

1. 新增 `ExecutionContext`、`BudgetLedger`、`CancellationToken`。
2. privacy-aware ModelRouter；restricted 任务禁止外部 rewrite/rerank/verify/synthesis。
3. 无本地合规路径时创建阶段返回 `PRIVACY_POLICY_UNSATISFIABLE`。
4. 将 time basis、license、classification 和 scope post-filter 接入所有新增 Evidence。
5. Deadline 贯穿 Verify 和模型 batch/retry。
6. 统一 source failure、deadline、candidate/read budget 的 stop/state 语义。
7. 给 dispatcher 增加队列容量、queue TTL、拒绝和统计。
8. 为 Research 增加 workload class 和独立并发配额，避免占满 Search 的 recall/ranking 容量。

已落地改动：

- 新增 [`research_execution.py`](../src/application/research_execution.py)，提供线程安全的 `BudgetLedger`、可提交/释放的预算预留、`CancellationToken`、`Deadline` 绑定的 `ExecutionContext`；round、候选、深读文档/页数/字节、模型请求和 token 都进入统一 usage。
- 新增 [`model_router.py`](../src/application/model_router.py) 和对应 port。standard 任务可以走配置的外部 rewrite/rerank/verify/synthesis，restricted 任务只允许本地或 disabled 路径；没有本地 verify 路径时在任务创建阶段返回 `PRIVACY_POLICY_UNSATISFIABLE`。
- 新增 [`research_policy.py`](../src/application/research_policy.py)，按四个 profile 固化 required source、验证 profile、独立来源、反证和信息增益饱和策略；policy 与 scope 不可满足时创建阶段返回 422。
- 新增 [`research_scope.py`](../src/application/research_scope.py)，统一执行 source type、published/updated/filing/publication time basis、language、jurisdiction、license 和 classification post-filter；seed 与后续新增 Evidence 使用同一门禁。
- Deadline 从 Research 传播到 Discovery、Recall、Ranking、Verify 和 entailment batch/retry；模型 HTTP timeout 取配置值与剩余 deadline 的较小值，超时后按结构化 stop/failure 收尾。
- [`research_dispatcher.py`](../src/application/research_dispatcher.py) 改为有界 admission：先 reserve 再持久化任务，支持 queue capacity、queue TTL、`Retry-After`、实时统计和稳定 429，不再使用无界提交。
- Bootstrap 为 Research 单独装配 recall/ranking executor；restricted Research 同时绕过共享 cache 的读写，避免原文或查询通过共享缓存越过 privacy 边界。
- 统一 `objective_satisfied`、`information_gain_saturated`、`max_rounds_reached`、`max_candidates_reached`、`source_failure`、`deadline_reached` 等 stop reason 与 completed/partial/failed 的映射；公开响应继续使用 provider-neutral `research.v1`。

实现验证：[`test_research_m0.py`](../tests/test_research_m0.py) 覆盖预算原子性、取消、全部 scope 字段、privacy 零外部调用、deadline、队列 TTL/429、独立资源池、restricted cache 隔离和 quick/standard/deep 收尾边界。

退出门槛：

- restricted 模式的外部模型 spy 调用数为 0；
- 所有可接受 scope 字段均有行为测试，否则创建时拒绝；
- quick/standard/deep 墙钟时间不超过 deadline 加固定收尾裕量；
- 队列过载稳定返回 429；
- 混合负载下 Research 不突破其资源配额；
- 当前 Research API 和 seed/hash 契约测试继续通过。

### M1：真实研究闭环

目标：从“固定多搜几次”升级为“gap 驱动”。

工作项：

1. 拆分 ResearchService 为 Coordinator、Runner、Planner、Coverage。
2. 引入 ObjectivePlan、CoverageTarget、ResearchAction、RoundResult。
3. 未提供 claims 时由 Planner 生成原子 claim，而不是把问题整体当 claim。
4. 接通 ClaimAssessment.followup_queries 和 CoverageGap。
5. 实现 source、claim、feature、time、language、jurisdiction、classification、license coverage。
6. 每轮保存 plan、action、实际 query/filter、来源结果、gain 和 usage。
7. 实现真实 `needs_input` 与结构化 feedback。

已落地改动：

- 将原大类拆成 [`research_coordinator.py`](../src/application/research_coordinator.py)、[`research_runner.py`](../src/application/research_runner.py)、[`research_planner.py`](../src/application/research_planner.py) 和 [`research_coverage.py`](../src/application/research_coverage.py)；`ResearchService` 保留为兼容门面。
- `ResearchCoordinator` 负责幂等创建、任务读取、feedback、cancel、队列提交和进程启动恢复；`ResearchRunner` 只执行一个 durable attempt，并在每个阶段检查取消、deadline 和预算。
- 新增 `ObjectivePlan`、`CoverageTarget`、`ResearchAction`、`CoverageGain`、`RoundResult` 和 `ResearchRoundCheckpoint`。未显式传 claims 时，Planner 对问题做确定性原子化并生成稳定 claim/action/gap ID。
- Coverage 按 source、claim、required feature、time、language、jurisdiction、classification 和 license 生成矩阵与 gap；Planner 只针对 gap 生成 search、counter_search、deep_read、citation_expand、family_expand 等 action。
- 每轮保存 action、实际 query/filter、来源结果、failure、coverage before/after、gain、usage、query trace、source snapshot、反证状态和 evidence set revision；停止由目标满足、预算或连续信息增益饱和决定。
- SQLite 增加 `research_attempts`、`research_evidence_sets`、`research_rounds` 和 `research_events`；round 与 evidence set 在一个事务中 checkpoint，恢复时从最后完整 round 继续，不重复采纳已提交轮次。
- prior-art 等存在关键歧义的目标进入真实 `needs_input`，返回 typed questions；带 `task_revision` 的 feedback 生成下一 plan revision/attempt，未消除歧义则继续 needs_input，消除后重新入队。
- 轮询可观察 planning、expanding、deep_reading、verifying、coverage_analysis 等 phase，以及 rounds、evidence、gap、scope exclusion 和 checkpoint 时间；终态转换带 revision guard。

实现验证：[`test_research_m1.py`](../tests/test_research_m1.py) 覆盖原子 claim、gap/action 可追溯性、八类 coverage、gain、事务 checkpoint、重启续跑、needs_input/feedback、取消和不可解引用 seed 降级。

退出门槛：

- 每个扩展 action 都能回溯到 gap；
- coverage 改善或信息增益饱和决定下一轮/停止；
- worker 在任意轮次重启后从 checkpoint 恢复；
- progress/phase 在轮询中可观察；
- completed/partial/failed/needs_input/cancelled 均有状态转移测试。

### M2：原文、Locator 与身份归并

目标：让关键 finding 真正可核验。

实施顺序：

1. Academic PDF cursor/locator/version；
2. Web 原页安全读取、content hash 和转载归并；
3. Patent claims/specification/family/citation。

已落地改动：

- 新增统一 [`DocumentReader`](../src/application/ports/document_reader.py) port，以及 `DocumentVersion`、`DocumentChunk`、`DocumentReadResult`、diagnostics 等内部合同；原文读取结果明确区分 ready/partial/failed、版本完整性、parser、许可和 storage mode。
- Academic reader 支持 PDF cursor 续读、page/chunk/char locator、content hash、parser version 和稳定 document version；只观察到部分 chunk 的版本标记为 incomplete，不能成为 qualified support。
- Web reader 使用 [`safe_web_fetch.py`](../src/infrastructure/safe_web_fetch.py) 执行 SSRF 防护、重定向逐跳校验、DNS/IP 检查、TLS、压缩比、响应体大小和总 deadline 限制；正文按 canonical paragraph 建立 content hash、version 和 locator。
- `noarchive`、许可不足、解析失败、重定向/网络安全拒绝等不会被静默丢弃，而是降级 evidence quality，并进入 diagnostics 与 coverage gap。
- Patent reader 与只读 fulltext ES adapter 支持 claims、specification 段落、publication/application/priority、family members、patent/NPL citations；Planner 在 prior-art profile 中按 family 再 citation 的顺序扩展。
- 新增 [`evidence_adoption.py`](../src/application/evidence_adoption.py)，只从稳定且完整的原文 chunk 生成 citable Evidence；abstract、snippet、provider extract 和关系发现记录只能用于 discovery，不能单独支持关键 claim。
- 新增 [`document_identity.py`](../src/application/document_identity.py)，分别计算 academic work/version、Web canonical/syndication/ownership 和 Patent family/version 身份；版本去重与独立来源计数分离，转载稿和同族成员不会虚增独立支持数。
- SQLite 增加 `research_document_reads` 和独立 `research_document_chunks`，全文不写入 task 单行 JSON；`resolve_locator` 按 research/version/chunk/char 范围解引用，Runner 在 verify 前再次降级不可解析 locator。
- Coverage/Planner 接入 abstract-only、原文不可用、版本不完整、许可、专利 claims/family/citation 等 gap，使深读和关系扩展仍由 gap 驱动并进入 round checkpoint。

实现验证：[`test_research_m2_academic.py`](../tests/test_research_m2_academic.py)、[`test_research_m2_web.py`](../tests/test_research_m2_web.py)、[`test_research_m2_patent.py`](../tests/test_research_m2_patent.py) 和 [`test_research_m2_identity.py`](../tests/test_research_m2_identity.py) 覆盖全文续读、安全抓取、版本/locator 解引用、许可降级、专利扩展与身份去重。

退出门槛：

- qualified finding 的 quote、locator、version 可 100% 解引用；
- abstract/snippet/provider extract 不会单独产生关键 claim 的 qualified support；
- 同一论文版本、专利族或转载稿不会虚增独立来源；
- OCR/解析/许可失败明确进入 diagnostics 和 coverage gap。

### M3：综合、Artifact 与质量校准

目标：交付既能给 Agent 使用，也能给人检查的研究结果。

工作项：

1. Dossier 增加 structured summary、key findings、conflicts、limitations、methods。
2. Synthesis 只能引用 qualified finding id；生成后执行 citation coverage audit。
3. 导出 Markdown、JSON、Evidence CSV/JSONL，artifact 使用受权访问和保留期。
4. 建立不少于 50 条人工标注 Research corpus。
5. 按 profile 评测 claim support、locator、identity、counterevidence、gap disclosure。

已落地改动：

- 扩展 `ResearchDossier`，增加 structured `summary`、`statements`、带稳定 ID 的 findings、`conflicts`、结构化 `limitations_detail`、`methods`、`citation_audit` 和 typed `artifact_index`；保留原 `artifacts` map 兼容 `research.v1`。
- 新增 [`research_dossier_builder.py`](../src/application/research_dossier_builder.py)，从 ClaimAssessment、Coverage、Boundary 和 resolved policy 确定性构建 Dossier；finding/statement/conflict/limitation ID 由稳定输入计算，结构化 Dossier 是综合与导出的唯一事实源。
- 新增内部 synthesis port/contract 和可选 SiliconFlow adapter。外部综合默认关闭，开启时最多一次请求、输入只含 qualified support/contradiction 和稳定 finding ID，最多每 finding 五条证据，HTTP timeout 受剩余 deadline 限制。
- factual synthesis 只能逐字复制单个 supported finding 的 claim 并引用该 finding；模型生成未知 finding、改写事实、遗漏 supported finding、遗漏冲突、产生无 locator 引用或返回非法结构时，统一回退到确定性综合。
- restricted 或禁用模型时不发生 synthesis egress；模型失败、deadline、引用审计失败也不影响结构化结果，Dossier 的 methods/failures 明确记录 deterministic、model 或 model_fallback。
- SQLite 增加 `research_syntheses`。Runner 在 egress 前保存 pending operation 和保守 usage intent，ready snapshot 保存 statements/audit/token；恢复遇到 pending 时不重放外部调用，遇到 ready 时重新审计当前 evidence，失败则本地修复。
- 新增 [`citation_audit.py`](../src/application/citation_audit.py)，对 factual/analysis statement、supported finding 覆盖、冲突双方、qualified relation、Evidence、quote、version 和 locator 失败关闭；审计未通过时禁止生成 artifact。
- 新增 [`research_artifacts.py`](../src/application/research_artifacts.py)，确定性导出 Dossier JSON、Markdown、Evidence CSV 和 Evidence JSONL；artifact ID/sha256 与 evidence revision/renderer version 绑定，重复执行幂等，CSV 对公式注入字符做转义。
- SQLite 增加 `research_artifacts` BLOB 与 metadata；`GET /research/{research_id}/artifacts/{artifact_id}` 沿用 API token 鉴权，返回 ETag、attachment、`nosniff` 和 private cache policy，并按 `RESEARCH_ARTIFACT_RETENTION_SECONDS` 对过期访问返回 410。
- 新增 `RESEARCH_SYNTHESIS_ENABLED`、`RESEARCH_SYNTHESIS_MODEL`、`RESEARCH_SYNTHESIS_TIMEOUT` 和 `RESEARCH_ARTIFACT_RETENTION_SECONDS` 配置；外部 synthesis 必须显式开启且配置 SiliconFlow key。
- `eval/golden/research_quality_corpus.json` 固定 50 条、每个 profile 至少 10 条的标注场景；[`research_quality_gate.py`](../eval/research_quality_gate.py) 按 profile 校验 status、claim support precision、locator、identity、counterevidence、冲突、gap 和 unsupported statement rate，并已接入统一 stability gate。
- tenant/principal 对象级授权、artifact 后台物理删除和跨实例 storage 仍属于 M4，不在 M3 单机基线中提前实现。

实现验证：[`test_research_m3.py`](../tests/test_research_m3.py) 覆盖稳定 ID、冲突保留、引用失败关闭、合法/非法模型综合、restricted 零调用、单次 deadline、配置门禁、operation 恢复、artifact 幂等/过期和鉴权下载。M0–M3 全量回归为 270 passed；Search 与 Research quality gate 均通过，50 条 Research corpus 的安全指标为 1.0，unsupported statement rate 为 0.0。

退出门槛：

- 生成报告无引用事实句为 0；
- 冲突不会被综合阶段静默消解；
- finding/evidence/locator/artifact 引用完整；
- 质量回归和成本/时延回归能阻止合并。

### M4：生产化与规模扩展

目标：支持多租户、多实例和明确 SLA。

工作项：

- 共享数据库、durable queue、lease/heartbeat；
- principal/tenant ownership 和对象级授权；
- Search/Research 独立 bulkhead、连接池、配额和 circuit；
- retention、删除、备份、加密和审计；
- `/livez`、`/readyz`、metrics、trace、stuck-task 告警；
- 可选 SSE/event hook，保留轮询作为基础协议。

退出门槛：

- kill/restart/duplicate delivery 不重复采纳或计费；
- 混合 Search/Research 负载下 Search P95 不突破登记阈值；
- accepted task 到合法终态比例、queue age、deadline/cancel 均可观察；
- 越权读取、取消和 artifact 访问测试全部拒绝。

## 12. 推荐 PR 序列

每个 PR 都应可独立测试和回滚：

1. `ExecutionContext + BudgetLedger`，先不改变行为。
2. Privacy ModelRouter 和创建期可满足性检查。
3. Scope post-filter 与 time basis。
4. Verify deadline/cancellation 和 stop semantics。
5. 有界 Research queue 与 queue metrics。
6. 拆 Coordinator/Runner，保持现有固定循环。
7. ObjectivePlan 与结构化 Coverage。
8. Gap → ResearchAction 和 round checkpoint。
9. Academic PDF cursor + locator。
10. IdentityResolver：Academic/Web/Patent 分步接入。
11. WebDocumentReader 安全实现。
12. PatentDocumentReader 与 family/citation 扩展。
13. Synthesis + citation audit + artifact。
14. 共享队列/数据库只在生产容量触发后实施。

不要在同一个 PR 中同时改公开 schema、研究循环、reader 和存储后端。

## 13. 测试与发布门禁

| 门禁 | 覆盖内容 |
|---|---|
| 单元/契约 | Policy/Privacy/Scope 拒绝矩阵；BudgetLedger；Planner/Coverage；Identity；Reader locator；Task state/revision/idempotency/ownership |
| 故障注入 | planning、provider、deep-read、verification、evidence commit、artifact 各阶段 kill/recover；不得重复 evidence、usage 或外部 operation |
| 混合负载（按需基准） | 在目标环境叠加 Search 与 quick/deep Research；观察 P95/P99、queue age、合法终态率、deadline 和 pool saturation；不作为默认合并/发布门禁 |
| Research 质量 | 独立有效证据召回、claim support precision、locator、版本/专利族归并、反证、无依据陈述、gap disclosure、可复现率及覆盖/时延/成本 |

安全硬门槛：restricted 外部原文模型调用、qualified relation 无效 locator、报告无引用事实句、未披露预算/来源/反证缺口均为 0。

## 14. 上线顺序

建议按能力成熟度逐个开放 Profile：

1. `technology_landscape beta`：强调覆盖地图，不宣称关键事实已验证；
2. `literature_review`：Academic locator 和版本归并达标后开放；
3. `technology_validation`：Web 原文、一手来源和反证达标后开放；
4. `prior_art_landscape`：Patent claims、日期口径和 family 达标后开放。

每个 Profile 使用独立 feature flag 和 policy version。旧任务始终保留创建时 resolved policy，不随服务端默认值漂移。

## 15. 完成定义

Research 功能完成不是“任务能跑完”，而是同时满足：

- 每个公开控制项都被执行或在创建时明确拒绝；
- 每轮动作都由目标或 gap 驱动并可审计；
- 每条关键 finding 能回到合格原文和稳定 locator；
- 独立来源按 work/family/ownership 计算，而非按 provider 数量；
- 冲突、失败、许可、预算和停止原因不会被隐藏；
- 任务可取消、可恢复、可重试且不会重复采纳或计费；
- 结构化 Dossier 是事实源，叙述性报告只是可校验派生物；
- 质量、可靠性、时延、成本和隐私都有自动发布门禁。
