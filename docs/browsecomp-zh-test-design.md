# chukonu-web-search BrowseComp-ZH 专项测试设计

| 文档项 | 内容 |
|---|---|
| 文档编号 | CHUKONU-WS-TEST-2026-003 |
| 版本 | v1.0 |
| 日期 | 2026-07-30 |
| 状态 | 设计完成，待实现与执行 |
| 对应模块 | T03 BrowseComp-ZH |
| 评测对象 | chukonu-web-search 中文复杂检索与多跳研究能力 |
| 上游报告 | [chukonu-web-search-evaluation-report.md](./chukonu-web-search-evaluation-report.md) |

## 1. 结论先行

BrowseComp-ZH 不能直接复用 FreshQA 的“原问题单次搜索 Top-8 → 固定模型回答”口径。官方基准的核心难点是持续改写查询、多轮检索、跨页核验和约束合取；只跑单轮搜索会把“不会继续找”与“搜索后端找不到”混在一起。

完整设计保留两个互不替代的评测轨道，但当前阶段只执行主轨：

1. **搜索后端隔离轨（主轨）**：固定规划/回答模型、网页读取器、提示词和预算，只替换搜索配置，衡量 Chukonu 检索结果对多跳 Agent 的净贡献。
2. **Chukonu 原生端到端轨（副轨，暂缓）**：未来评估 `search → research → 固定回答模型` 的完整能力；结果代表 Chukonu 搜索与研究编排的组合，不解释为纯搜索后端成绩。

主指标为经参考答案有效性审计后的短答案准确率 `Accuracy_valid`。同时报告无搜索增益、相对单轮检索增益、证据支持率、校准误差、延迟、成功率和成本。首次正式运行用于建立基线，不预设缺乏依据的绝对通过分数。

## 2. 对现有评估报告的审阅

### 2.1 可直接复用的做法

- 固定数据快照、文件哈希、模型、Judge、Top-K、证据预算和评测日期。
- 点估计同时给出 Wilson 95% 置信区间。
- 不把不同批次、不同生成链路的点估计差值解释为严格配对差异。
- 将搜索成功率、`complete`、P50/P95、证据量和成本与答案质量一起报告。
- T03 在执行前保持“待测”，不从 FreshQA 结果推导复杂检索能力。

### 2.2 T03 必须补齐的内容

| 缺口 | 风险 | 本设计的处理 |
|---|---|---|
| FreshQA 只有单轮检索 | 无法测试迭代搜索和多跳推理 | 增加固定 Agent 多轮检索主轨 |
| 搜索后端与研究 Agent 边界未定义 | 无法判断增益来自检索还是编排 | 主轨隔离后端，副轨单列原生端到端 |
| 未固定搜索轮数、网页读取、Token 和时间预算 | 测试时计算量不同会主导结果 | 预注册 Standard 预算 |
| 未定义 BrowseComp-ZH 判分协议 | 与官方数字或 FreshQA Judge 混用会失真 | 单列官方兼容分与内部审计分 |
| 未处理动态参考答案 | 失效参考答案会把正确系统判错 | 运行前盲化审计 289 条参考答案 |
| 未记录搜索轨迹和最终支持证据 | 只能知道答错，无法定位检索链路故障 | 冻结查询、结果、URL 读取和引用轨迹 |
| 未设置公开基准泄漏防护 | 搜索可能直接命中题目或答案镜像 | 预注册泄漏 URL 清单并记录过滤事件 |

## 3. 基准快照与使用约束

### 3.1 固定快照

| 项目 | 固定值 |
|---|---|
| 官方仓库 | `PALIN2018/BrowseComp-ZH` |
| 仓库 commit | `86abe635e7deef89ec00c68ff1c2588f0e2f2099` |
| 数据文件 | `data/browsecomp-zh-encrypted.xlsx` |
| 数据 SHA-256 | `49963cdc8b4a16f4656bbac89ed5f3495f7b3bec4cf310990f567e7893c6a531` |
| Split | `test` |
| 样本量 | 289 |
| 字段 | `Topic`、`Question`、`Answer`、`canary` |
| 语言 | 原生中文 |

数据只有一个 `test` split，没有训练集、开发集、稳定样本 ID 或随数据发布的参考证据 URL。评测工具应以
`sha256(question)` 生成稳定 `sample_id`，但不得把解密后的题目、答案或 canary 提交到仓库。

### 3.2 领域分布

| 领域 | n | 占比 |
|---|---:|---:|
| 影视 | 45 | 15.6% |
| 艺术 | 40 | 13.8% |
| 地理 | 37 | 12.8% |
| 音乐 | 32 | 11.1% |
| 历史 | 29 | 10.0% |
| 医学 | 26 | 9.0% |
| 电子游戏 | 23 | 8.0% |
| 科技 | 22 | 7.6% |
| 体育 | 18 | 6.2% |
| 政策法规 | 10 | 3.5% |
| 学术论文 | 7 | 2.4% |

总分按问题宏平均；领域分数只作诊断并同时给出 `n`，不得把小领域的点估计解释为稳定差异。

### 3.3 基准事实与协议边界

- 官方论文说明，题目从短、客观答案逆向构造，初始 480 条经过难度和唯一性筛选后保留 289 条。
- 构造阶段要求每题至少有一个权威来源支撑，但发布数据字段中没有该来源 URL，因此正式评测必须自行重新核验参考答案。
- 官方论文的模型评测要求输出 `Explanation / Exact Answer / Confidence`，使用 GPT-4o 按参考答案判断语义等价，并报告 Accuracy 和五桶 ECE。
- 论文中的 AI 搜索产品通过人工 GUI 操作和人工答案提取完成，没有公开统一的工具调用次数、Token、网页读取量和超时预算。本文结果因此不能与论文产品分数作严格同协议排名。

### 3.4 许可与数据治理

截至固定 commit，GitHub README 声称 MIT，但仓库中未包含其链接指向的 `LICENSE` 文件；Hugging Face 数据卡标记为 Apache-2.0，README 又限定为学术研究用途。三者不完全一致。

执行前应由数据负责人确认内部评测用途和可接受的使用范围。未确认前按更严格口径处理：

- 仅用于内部研究评测，不再分发数据集。
- 解密文件置于仓库外的受限临时目录，默认权限 `0600`。
- CI、日志、报告和 Git 中不保存明文题目、答案、canary 或完整模型响应。
- 可公开的报告只包含聚合指标、匿名 `sample_id` 和经脱敏的失败类型。

## 4. 评测目标与非目标

### 4.1 目标

1. 测量当前四源 Chukonu 对固定 Agent 的准确率提升。
2. 区分参数记忆、单轮检索和多轮检索的贡献；原生研究编排留待副轨实施。
3. 定位失败发生在查询分解、召回、排序、网页读取、证据核验、推理还是答案格式。
4. 给出质量、延迟、可用率和成本的同口径结果，并建立后续回归基线。

### 4.2 非目标

- 不把内部结果称为 BrowseComp-ZH 官方榜单成绩。
- 不用公开论文中的产品分数作为发布门槛。
- 不在测试集上调提示词、挑选模型、修改融合权重或人工重试个别题目。
- 不用 BrowseComp-ZH 替代 FreshQA、检索相关性、稳定性、安全或成本专项测试。
- 不将原生 `research` 的结果解释为搜索后端单独贡献。

## 5. 对照组与归因边界

### 5.1 必跑配置

| ID | 配置 | 工具能力 | 回答的问题 | 归因 |
|---|---|---|---|---|
| B0 | 固定模型，无搜索 | 无 | 模型参数记忆能答多少 | 模型基线 |
| B1 | 四源 Chukonu 单轮 | 原问题搜索一次，Top-8 | 复用 FreshQA 式链路能答多少 | 搜索 + 固定回答模型 |
| B2 | 四源 Chukonu 多轮 Agent | 固定 Agent 可改写查询、搜索、读页 | 多轮策略相对单轮带来多少增益 | **搜索后端主结果** |

B0–B2 必须使用同一固定回答模型。B2 的规划模型也固定为该模型；若它不能稳定遵循工具协议，允许在正式运行前一次性更换，但三个配置和全部单源对照必须同步更换并重新开始。

初始建议复用 FreshQA 的 `Qwen/Qwen3-30B-A3B-Instruct-2507`，温度设为 0。正式配置必须记录精确模型 ID、服务商、解码参数和请求时间。

### 5.2 推荐消融

| ID | 配置 | 用途 |
|---|---|---|
| A1 | Doubao 单源多轮 Agent | 与现有 FreshQA 单源结果衔接 |
| A2 | Aliyun 单源多轮 Agent | 比较四源 Chukonu 与 Aliyun 单源的同协议差异 |
| A3 | Baidu 单源多轮 Agent | 比较四源 Chukonu 与 Baidu 单源的同协议差异 |

A1–A3 仅作为独立单源搜索后端对照，不做从四源 Chukonu 中逐项去源或修改排序算法的消融；四源组成、融合与重排均视为当前 Chukonu 搜索后端的整体能力。

合成冒烟中的 A1–A3 通过评测器内的单源适配器直接调用仓库现有 provider，
再统一映射为 `title / url / snippet / published_date / source`。它们不经过
Chukonu 四源融合和重排，也不向公开 `/search` 接口增加 provider 调参字段。每次
search 事件必须记录声明 backend 和实际返回的 source；只要出现混源、来源缺失或
backend 不匹配，该单源轨即失败。

外部 AI 搜索产品若无法共享规划模型、读取器和预算，只能进入“产品级参考表”，不得与 B2 放在搜索后端隔离榜中。

## 6. 工具和 Agent 协议

### 6.1 B2 固定 Agent 工具

B2 只向 Agent 暴露两个评测专用工具：

```text
search(query, limit=10, source_types=["web"])
open_url(ref, max_chars)
```

- `search` 适配不同后端并返回统一的 `title / url / snippet / published_date / source`。
- 每条 search 结果由 EvidenceRegistry 分配 `s1...sN`；`open_url` 只接受这些
  ref，由评测器确定性解析对应 URL。工具 schema 会枚举当前可用 ref，模型不能
  生成或改写 URL；读取失败的 ref 从后续枚举中移除。
- `open_url` 使用所有后端共享的网页读取器，不使用搜索供应商私有生成答案。
- 搜索结果和网页正文均视为不可信数据，网页中的指令不得改变评测任务、预算或输出格式。
- Agent 只能看到当前问题和自己的工具轨迹，不能访问文件系统、Shell、参考答案、Judge、其他配置结果或历史样本。
- 工具重试由评测器控制，Agent 不能通过重复同一失败请求绕过预算。

`open_url` 是评测器内部工具，不是仓库的统一公开搜索接口；B2 与 A1–A3 已使用
同一个 PageReader 实现。

### 6.2 B3 原生研究协议（暂缓）

B3 不进入当前 Pilot、Final、统计比较和验收范围。以下协议仅作为后续实施预留：

1. 用原问题调用 `search`，固定 `limit=10`、`source_types=["web"]`。
2. 使用返回的 `search_id` 启动 `research`。
3. 轮询到 `completed`、`failed`、`cancelled` 或 Deadline。
4. 若进入 `needs_input`，统一回答“按原问题继续，目标是找到唯一、可验证的短答案”，不得人工给提示。
5. 将最终 dossier 交给固定回答模型，不直接把内部研究结论当 Judge 输入。
6. 保存 `seed_snapshot_hash`、`resolved` 预算、停止原因、证据索引和失败列表。

B3 Standard 预算固定为最多 3 轮、100 个候选、10 次深读和 120 秒研究 Deadline；实际服务返回的 `resolved.budget` 超出任一上限时，该样本标记 `budget_violation`，整批不得用于公平比较。

### 6.3 输出格式

所有配置必须输出同一 JSON：

```json
{
  "status": "answered",
  "exact_answer": "简短最终答案",
  "confidence": 73,
  "explanation": "说明如何满足问题约束，不引入无关事实",
  "evidence": [
    {
      "url": "https://example.com/source",
      "quote": "直接支持最终答案或关键约束的短摘录"
    }
  ]
}
```

规则：

- `status` 只允许 `answered` 或 `not_attempted`。
- `confidence` 为 0–100 的整数。
- Judge 只比较 `exact_answer` 与参考答案；解释和引用另做支持性审计。
- `not_attempted` 在主准确率中计错，同时单列拒答率。
- 无法解析的输出允许一次确定性格式修复，修复器不得看到参考答案。

## 7. 预算与运行规模

### 7.1 固定预算

| 预算 | 搜索调用 | 每次 Top-K | URL 读取 | 累计证据字符 | 单题墙钟时间 | 用途 |
|---|---:|---:|---:|---:|---:|---|
| No-search | 0 | — | 0 | 0 | 60 s | B0 |
| Single | 1 | 8 | 0 | 12,000 | 60 s | B1 |
| Standard | 最多 8 | 10 | 最多 12 | 80,000 | 180 s | B2 主结果 |

所有预算还必须记录规划/回答模型的输入、输出和缓存 Token。供应商不返回 Token 时标记 `unavailable`，不得估算为 0。

### 7.2 运行阶段

| 阶段 | 样本 | 目的 | 是否进入正式结论 |
|---|---:|---|---|
| Harness smoke | 合成题 5 条 | 验证工具、预算、日志、Judge 和泄漏过滤 | 否 |
| Pilot | 分层固定 55 条，每领域 5 条 | 估算成本、限流和故障率 | 只作运行诊断 |
| Final Standard | 全部 289 条 | B0–B2 主结果 | 是 |
| Variance | 分层固定 55 条 × 3 次 | 测量活网页面和 Agent 随机性 | 是，单独报告 |

Pilot 前必须冻结模型、Prompt、工具 schema、预算、重试和 Judge。Pilot 后只能修复使测试无效的工程故障；任何质量调优都需要废弃该批次、记录原因并重新冻结。

当前 Pilot 的固定模型输出上限为：Planner 700、Finalizer 700、Judge 180
Token。Finalizer Prompt 要求 `exact_answer` 只写答案本身，`explanation` 在
schema 中固定为 `""`；解释不参与短答案判分。若 Finalizer 返回不可解析 JSON，
或违反终态/证据引用不变量，允许带纠错提示重试一次，调用和 Token 全量计费。
Judge 同样使用最小严格 schema，
`reason` 固定为 `""`，遇到无效 JSON 最多重试一次。当前 SiliconFlow 严格 schema 解码对
`maxLength / maxItems` 存在长时间无响应，因此不使用这些关键字；字段、类型、
状态和引用仍由 schema 与本地解析器双重校验。

Judge 的模型通道只接收 `status=answered` 的候选，因此输出枚举只有
`CORRECT / INCORRECT`；`NOT_ATTEMPTED` 仅由评测器根据候选 status 确定性产生。

## 8. 参考答案有效性与泄漏控制

### 8.1 参考答案审计

官方论文明确承认动态网页会影响答案稳定性；公开的 BrowseComp-ZH-revised 项目还报告了 24 条疑似错误或不当答案。修订集不是官方真值，不能直接覆盖官方答案，但足以触发全面审计。

Final 前由两名审阅者在看不到任何系统输出的情况下独立核验 289 条：

| 状态 | 定义 | 计分处理 |
|---|---|---|
| `valid_original` | 官方答案仍唯一且有可靠证据 | 纳入 official/raw 与 valid 分 |
| `valid_updated` | 问题仍有效，但当前唯一答案已变化或官方答案有误 | raw 用原答案；valid 用审定答案 |
| `ambiguous` | 当前存在多个满足全部约束的答案 | 从 `Accuracy_valid` 分母排除 |
| `unverifiable` | 关键来源消失或无法证明唯一答案 | 从 `Accuracy_valid` 分母排除 |

两名审阅者分歧必须由第三人裁决。每条保留核验日期、1–3 个来源 URL、支持摘录、结论和原因。审阅者不得参与同批模型答案的人工裁决。

最终同时报告：

- `Accuracy_official_raw`：289 条、官方原答案、官方兼容 Judge。
- `Accuracy_valid`：只在 `valid_original + valid_updated` 上计算，使用审定答案。
- `invalid_reference_rate`：`ambiguous + unverifiable` 占比。
- `updated_reference_rate`：`valid_updated` 占比。

不得只挑对某个系统有利的一个口径。

### 8.2 公开基准泄漏

由于题目已公开且可解密，搜索结果可能命中题目镜像、答案表、评测日志或修订仓库。主分采用预注册的泄漏过滤：

- 阻断官方数据分发页、论文样例页、修订数据、排行榜答案页和已知镜像的具体 URL/path；不因域名中有 GitHub 或 Hugging Face 就阻断整个域。
- 对结果标题、snippet 和正文检测问题长片段重合、canary 和“BrowseComp-ZH + 答案”等泄漏信号。
- 被过滤结果仍保存在受限审计日志中，但不交给 Agent。
- 若新的泄漏页在运行中被发现，冻结整批、更新清单并从头重跑全部配置，不能只重跑受影响系统。

同时报告 `leak_hit_rate` 和无过滤的敏感性结果；无过滤结果不得作为主结论。

## 9. 判分协议

### 9.1 两条计分通道

**官方兼容通道**

- 使用官方仓库的预测格式和 Judge Prompt。
- Judge 使用官方论文指定的 GPT-4o；记录实际模型 ID、服务商、温度、`top_p` 和日期。
- 若只能使用会漂移的模型别名，必须明确披露，结果称为 `official-compatible` 而不是官方复现。
- 该通道只产生 `Accuracy_official_raw` 和官方五桶 ECE。

**内部审计通道**

1. 先做 Unicode NFKC、大小写、空白和常见全半角标点归一化。
2. 只对明确别名、数字/日期等价和可审计单位换算做确定性匹配。
3. 其余样本由固定 Judge 在盲化系统身份后输出 `CORRECT / INCORRECT / NOT_ATTEMPTED`。
4. 随机分层抽取至少 20%，加上全部 Judge 分歧、边界数值和 `valid_updated` 样本做人工复核。
5. Judge 与人工分歧经裁决后形成 `Accuracy_valid`。

不得让 Judge 联网或看到系统名称、搜索轨迹、置信度、参考答案审计状态和其他系统回答。Judge 只看问题、审定参考答案和候选 `exact_answer`。

### 9.2 证据审计

答案正确和证据充分是两个独立维度：

- `answer_support_rate`：最终答案是否被至少一个所引来源直接支持。
- `constraint_coverage`：问题中的关键约束有多少被引用证据覆盖。
- `citation_precision`：所引 URL 中真正支持答案或约束的比例。
- `independent_source_support`：是否有两个独立发布主体交叉支持。

所有正确答案和分层抽取的错误答案进入证据审计。只答对但引用不支持的样本仍计入 Accuracy，但证据指标失败。

## 10. 指标与统计

### 10.1 主指标

| 指标 | 定义 |
|---|---|
| `Accuracy_valid` | `CORRECT / 有效参考答案数` |
| `SearchLift` | 同题 `B2 Accuracy_valid - B0 Accuracy_valid` |
| `IterationLift` | 同题 `B2 Accuracy_valid - B1 Accuracy_valid` |

系统失败、超时、空回答和不可解析回答在端到端 Accuracy 中计错；只有参考答案无效或 Judge 无法完成且尚未人工裁决的样本可从分母排除。

### 10.2 次指标

| 类别 | 指标 |
|---|---|
| 官方兼容 | `Accuracy_official_raw`、五桶 `ECE` |
| 校准 | `ECE_valid`、Brier score、置信度解析率 |
| 证据 | answer support、constraint coverage、citation precision、独立来源支持率 |
| 检索过程 | 搜索轮数、查询改写数、候选 URL、唯一域名、深读数、首次支持证据所在轮次 |
| 运行 | success、complete、timeout、provider failure、retry、budget violation |
| 性能 | 端到端 P50/P95/P99、搜索/读页/模型/Judge 分段延迟 |
| 成本 | 每题成本、每个正确答案成本、各供应商/模型成本构成 |

不把这些指标压成单一加权总分。

### 10.3 统计方法

- 单系统 Accuracy 使用 Wilson 95% CI。
- 同题系统差值使用按领域分层的 paired bootstrap 95% CI，固定 10,000 次重采样。
- 二分类配对差异同时报告 McNemar exact test；多组比较使用 Holm 校正。
- 报告绝对百分点差、相对变化、共同答对、只 A 答对、只 B 答对和共同答错。
- 领域结果只给点估计和区间，不对极小分桶单独宣称显著性。
- Variance 子集报告三次均值、标准差和逐题一致率。

## 11. 公平运行与可复现性

1. B0–B2 使用同一数据顺序和同一参考答案快照。
2. 联网配置按题交错运行，配置顺序采用固定 Latin square，避免某个配置总在更早的网页状态下运行。
3. Final Standard 在连续 48 小时 UTC 窗口内完成；超出则整批标记为跨窗，不与同窗批次作严格配对。
4. 固定客户端并发、连接池、超时、重试次数和退避策略；429 不得通过无限重试掩盖。
5. 原始搜索结果、网页读取、工具事件和停止原因即时落盘；重跑默认复用冻结数据，只有“live rerun”才能重新访问公网。
6. Prompt、工具 schema、配置、代码 commit、依赖锁、数据 hash、Judge 和泄漏清单全部进入 manifest。
7. B2 与外部后端对比时统一 Top-K 和网页读取器；供应商私有答案不得混入检索后端轨。

## 12. 失败分类

每个错误样本只能指定一个主因，可附多个次因：

| 代码 | 主因 |
|---|---|
| `GT_INVALID` | 参考答案变化、歧义或不可核验 |
| `QUERY_DECOMPOSITION` | 未识别关键约束或未生成有效子查询 |
| `RECALL_MISS` | 所有查询都未召回关键页面 |
| `RANKING_MISS` | 关键页面已召回但未进入 Agent 可见 Top-K |
| `PAGE_ACCESS` | 登录墙、反爬、死链、脚本渲染或读取失败 |
| `EVIDENCE_EXTRACTION` | 页面存在答案但正文/片段未提取到 |
| `SOURCE_CONFLICT` | 选择了过时、低质量或冲突来源 |
| `REASONING` | 证据充分但约束合取、消歧或推理错误 |
| `ANSWER_FORMAT` | 结论正确但 `exact_answer` 缺失、含歧义或无法解析 |
| `PROMPT_INJECTION` | 网页指令影响工具策略或输出 |
| `OPERATIONAL` | 超时、限流、服务失败或预算中断 |
| `LEAKAGE` | 依赖题库、答案镜像或其他评测产物 |

报告至少列出各主因计数、按领域分布、按搜索来源分布，以及 B1→B2 被修复和被破坏的样本数。

## 13. 产物与数据结构

建议新增但不提交敏感内容的实现路径：

```text
eval/
├── browsecomp_zh_eval.py
├── browsecomp_zh_pilot.py
├── browsecomp_zh_synthetic.json
├── browsecomp_zh_smoke_details.json
├── browsecomp_zh_smoke_report.md
├── browsecomp_zh_single_source_smoke_details.json
├── browsecomp_zh_single_source_smoke_report.md
├── browsecomp_zh_judge.py
├── browsecomp_zh_ground_truth.py
├── browsecomp_zh_reporting.py
├── browsecomp_zh_configs.yaml
└── browsecomp_zh_leak_blocklist.txt
```

受限运行目录：

```text
<secure-run-dir>/
├── dataset_manifest.json
├── ground_truth_audit.jsonl
├── run_manifest.json
├── items.jsonl
├── raw_tools/
├── raw_pages/
├── judgments.jsonl
├── failure_analysis.jsonl
└── report.md
```

单题记录最少包含：

```json
{
  "sample_id": "sha256:...",
  "topic": "科技",
  "ground_truth_status": "valid_original",
  "system_id": "B2",
  "run_status": "completed",
  "answer_status": "answered",
  "correct": true,
  "confidence": 73,
  "tool_counts": {
    "search": 5,
    "open_url": 6
  },
  "latency_ms": {
    "total": 84231,
    "search": 12600,
    "open_url": 9100,
    "model": 61500
  },
  "budget_violation": false,
  "leak_hits": 0,
  "failure_code": null
}
```

明文答案、解释、URL 摘录和完整工具轨迹放在受限子文件中；聚合报告只按 `sample_id` 引用。

## 14. 验收与回归门槛

### 14.1 首次正式运行

首次运行的目标是建立审定基线，不设置没有历史依据的最低 Accuracy。以下任一情况会使整批 `invalid`：

- 数据 hash、代码 commit、模型、Prompt 或预算未冻结。
- 289 条中存在未解释的缺失、重复或错配 ID。
- Agent、搜索或 Judge 接触到参考答案。
- 未解决 Judge 失败或人工裁决分歧。
- 发生预算越界、泄漏清单中途修改却未全量重跑。
- B0–B2 未在同一运行窗口完成，却被当作严格配对比较。

### 14.2 运行健康门槛

- `success_rate >= 98%`。
- Standard `budget_violation_rate = 0`。
- Standard 端到端 `P95 <= 180 s`。
- Judge 和输出解析最终完成率为 100%。
- 成本明细覆盖率为 100%，供应商确实不提供的字段可标 `unavailable`。

运行失败仍在 Accuracy 中计错；健康门槛用于阻止把严重故障批次当作质量结论。

### 14.3 后续回归门槛

首次基线审定后，后续变更默认阻断条件：

- `Accuracy_valid` 点估计下降超过 2 个百分点，且 McNemar Holm 校正后 `p < 0.05`。
- `answer_support_rate` 下降超过 3 个百分点。
- `success_rate < 98%`、P95 超预算或出现任何预算越界。
- 单题平均成本上升超过审定预算，且没有预注册的质量收益目标。

候选改进可在 paired Accuracy 95% CI 下界大于 0、证据质量不退化且运行健康门槛通过时认定为明确增益；否则表述为“未观察到确定增益”。

## 15. 建议复现命令

基础轨、单源轨合成冒烟及 55 条 Pilot 接口已经实现；Final 和独立聚合报告命令仍是待实现接口，不代表仓库当前已具备对应参数：

```bash
# 只验证协议，不使用正式题目
.venv311/bin/python -m eval.browsecomp_zh_eval --synthetic-smoke

# 验证 A1 Doubao、A2 Aliyun、A3 Baidu 单源隔离轨
.venv311/bin/python -m eval.browsecomp_zh_eval --single-source-smoke

# 分层 Pilot，只做运行诊断
.venv311/bin/python -m eval.browsecomp_zh_pilot \
  --dataset <restricted-data-dir>/browsecomp-zh-encrypted.xlsx \
  --sample-size 55 \
  --per-topic 5 \
  --seed 20260730 \
  --secure-run-dir <restricted-run-dir>

# 同一批 55 条的 A1 Doubao 单源多轮 Agent Pilot
.venv311/bin/python -m eval.browsecomp_zh_doubao_pilot \
  --dataset <restricted-data-dir>/browsecomp-zh-encrypted.xlsx \
  --sample-size 55 \
  --per-topic 5 \
  --seed 20260730 \
  --secure-run-dir <restricted-doubao-run-dir>

# Final Standard
.venv311/bin/python -m eval.browsecomp_zh_eval \
  --config eval/browsecomp_zh_configs.yaml \
  --systems B0,B1,B2 \
  --all \
  --budget standard \
  --freeze-live-results

# 生成聚合报告
.venv311/bin/python -m eval.browsecomp_zh_reporting \
  --run-dir <secure-run-dir> \
  --paired-bootstrap 10000 \
  --wilson-ci
```

## 16. T03 结果回填模板

```json
{
  "module_id": "T03",
  "benchmark": "BrowseComp-ZH",
  "status": "pending",
  "evaluation_date": null,
  "dataset": {
    "repo_commit": "86abe635e7deef89ec00c68ff1c2588f0e2f2099",
    "sha256": "49963cdc8b4a16f4656bbac89ed5f3495f7b3bec4cf310990f567e7893c6a531",
    "split": "test",
    "sample_size": 289
  },
  "method": {
    "systems": ["B0", "B1", "B2"],
    "planner_answer_model": null,
    "official_compatible_judge": null,
    "internal_judge": null,
    "budget": "standard",
    "leak_filter_version": null
  },
  "ground_truth": {
    "valid_original": null,
    "valid_updated": null,
    "ambiguous": null,
    "unverifiable": null
  },
  "metrics": {
    "primary": "Accuracy_valid",
    "b0": null,
    "b1": null,
    "b2": null,
    "b3": null,
    "search_lift_pp": null,
    "iteration_lift_pp": null,
    "official_raw_accuracy": null,
    "answer_support_rate": null
  },
  "runtime": {
    "success_rate": null,
    "complete_rate": null,
    "p50_ms": null,
    "p95_ms": null,
    "p99_ms": null,
    "cost_per_query": null,
    "cost_per_correct_answer": null
  },
  "limitations": []
}
```

只有数据审计、全量运行、Judge 裁决和运行健康门槛全部完成后，`status` 才能从 `pending` 改为 `completed`。

## 参考

- [BrowseComp-ZH 论文](https://arxiv.org/abs/2504.19314)
- [BrowseComp-ZH 官方仓库](https://github.com/PALIN2018/BrowseComp-ZH)
- [BrowseComp-ZH Hugging Face 数据集](https://huggingface.co/datasets/PALIN2018/BrowseComp-ZH)
- [BrowseComp-ZH-revised：非官方参考答案修订](https://github.com/AGI-Eval-Official/BrowseComp-ZH-revised)
- [BrowseComp 原始论文](https://arxiv.org/abs/2504.12516)
- [项目现有中文 Web 评测设计](./web-search-eval-design-zh.md)
- [项目现有评测方法](./eval-methodology.md)
