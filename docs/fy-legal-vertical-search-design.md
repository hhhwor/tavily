# FY 法律垂直搜索接入设计

> 状态：设计提案，尚未实现
>
> 日期：2026-08-10
>
> 范围：在现有 `web-search` 的 `/search` 与 MCP `search` 中接入法研（FY）法律检索；不改变现有通用 Web、Academic、Patent 搜索语义。

## 1. 结论

将 FY 接入为一个受法律意图路由控制的复合 Web 来源 `fy_legal`，而不是把每个 FY 接口无条件注册成普通 Web provider。默认以法规和相似问答为主召回，按需补充实务文章和网络观点；流式“法律法规咨询”不混入检索 evidence，而作为后续可选的独立回答能力。

这样可以保留 `/search` 的“可定位证据检索”边界，避免每个普通 Web 查询产生额外法律接口费用，并使法规、问答、实务材料的来源等级与限制可被记录。

本设计基于宁夏部署机完成的真实 FY 调用。以下非流式接口均返回 HTTP 200 和业务码 `1000`：

| FY 能力 | service_id | 网关路径 | 返回集合 | 在垂直搜索中的定位 |
|---|---:|---|---|---|
| 法律法规知识库 | `1922892755967496194` | `/llm/rag/open/v1/law_view` | `data.laws` | 首选法规依据 |
| 相似疑问知识库 | `1922894917724360705` | `/llm/rag/open/v1/similar_question` | `data.qa` | 高频问题与通俗解释 |
| 网络观点知识库 | `1922894118774165505` | `/llm/rag/open/v1/web_view` | `data.web` | 观点与补充背景 |
| 实务文章知识库 | `1922893369225072641` | `/llm/rag/open/v1/t2wechat_view` | `data.t2wechat` | 实务解读与案例线索 |
| 法律法规咨询（流式） | `1876814391735312386` | `/defg/web/openapis/template/flux/38` | SSE `choices[].delta.content` | 不进入首期检索 evidence |

宁夏机器对 `api.cjbdi.com:8443` 的 TCP 连接超时；同域名 443 的相同路径可以完成上述调用。因此运行配置必须显式使用可用的 API 基地址，不在代码中硬编码端口或静默跨端口回退。

## 2. 目标与非目标

### 2.1 目标

1. 对中国法律法规、法律实务和法律问答意图，返回比通用网页搜索更相关的法规、问答和实务材料。
2. 保持现有 `SearchCommand → QueryPlanner → RecallCoordinator → Ranking → Evidence` 主链、Deadline、熔断、缓存和失败契约。
3. 以可配置、可审计的方式使用 FY 的 `AccessToken + AppID + MD5` 请求签名，绝不把 app secret、access token、签名或原始敏感 query 写入普通日志和公开响应。
4. 明确区分“法规检索结果”“相似问答/实务材料”和“模型生成咨询”；不把后两者包装为法律结论或权威法条。
5. 对非法律或非中国法域查询不额外调用 FY；调用方可显式选择法律垂直搜索。

### 2.2 非目标

- 不提供律师意见、侵权/FTO/有效性判断或个案法律结论。
- 不把 FY 流式模型输出作为 `/search.evidence`，也不由它替代 Research 的 claim verification。
- 不新增 `DocumentKind="legal"`。首期复用 `web`，避免在 SearchService、Ranking、Evidence、Research 中增加一套平行领域分支。
- 不在本次接入中实现跨法域法律库、法律版本比对、法规全文下载或人工审阅工作流。

## 3. 当前约束与接入原则

### 3.1 当前架构约束

- `SourceRegistry` 以 `SourceDescriptor.kind` 注册来源；`QueryPlanner` 目前会把全部 Web source 放进计划，`RecallCoordinator` 随后并发调用它们。
- `SearchRequest` 只公开 `query`、`limit`、`source_types`、`filters`；`jurisdictions` 是请求边界，不应被未真正支持的 provider 宣称为已过滤。
- Provider 级缓存键包含 query、时间、语言和辖区；`RetrievalBatch` 必须记录实际过滤、snapshot、限制与失败。
- `/search` 返回的是发现型 evidence。现有公共 Presenter 会移除内部 provider 标识，故用户可见引用应依靠 title、venue、URL 与内容类别，而非暴露凭据供应商名称。

### 3.2 接入原则

1. **显式优先**：调用方显式选择 `legal` 时始终尝试 FY；自动路由只补充高精度法律意图。
2. **法规优先**：`law_view` 是默认主来源；问答、实务和网络观点只能补充或解释，不能覆盖法规结果的语义地位。
3. **真实边界**：FY 当前测试的检索接口没有日期、语言或调用方传入辖区过滤参数。适配器必须报告这些过滤未被应用。
4. **令牌单飞**：FY 重复获取 accessKey 会使先前 accessKey 失效。不得每次搜索获取令牌，更不能在多副本间各自无协调刷新。
5. **失败可降级**：FY 部分或整体不可用时，通用 Web 搜索继续完成；只有显式法律请求才返回清晰的法律来源不可用诊断。

## 4. 总体架构

```mermaid
flowchart LR
    REST[REST /search] --> CMD[SearchCommand]
    MCP[MCP search] --> CMD
    CMD --> PLAN[QueryPlanner + 法律意图路由]

    PLAN -->|普通 web sources| WEB[腾讯 / 百度 / 豆包 / 阿里云]
    PLAN -->|vertical=legal 或高置信法律意图| FY[FyLegalSearchProvider]

    FY --> AUTH[FyAccessTokenProvider]
    AUTH --> TOKEN[/auth/api/token/getAccess]
    FY --> LAW[law_view: 法规]
    FY --> QA[similar_question: 问答]
    FY --> PRACTICE[t2wechat_view: 实务]
    FY --> OPINION[web_view: 网络观点]

    WEB --> RECALL[RecallCoordinator]
    FY --> RECALL
    RECALL --> RANK[Web Ranking + 法律来源配额]
    RANK --> EVIDENCE[Evidence / RetrievalBoundary]

    CONSULT[flux/38 SSE 法律咨询] -.独立后续能力，不进入 .-> EVIDENCE
```

### 4.1 复合 provider，而非五个常驻 provider

选择一个 `FyLegalSearchProvider`（`descriptor.id="fy_legal"`, `kind="web"`）作为 FY 的唯一 Registry 来源。它在内部根据检索计划并发或顺序调用多个 FY endpoint，并以 `knowledge_type` 保存每条候选的类别。

不直接把四个非流式 endpoint 分别注册到 Registry，原因是当前 planner 会对每一个 Web provider 无条件执行，导致非法律查询产生四次额外调用；同时也会使 `per_provider_k`、缓存、失败统计和成本上限失控。

## 5. 路由与公开输入

### 5.1 新增垂直选择字段

在保持旧请求兼容的前提下，扩展 `SearchCommand`、REST `SearchRequest` 与 MCP `search` schema：

```json
{
  "query": "不满两周岁子女离婚后由谁直接抚养？",
  "limit": 10,
  "verticals": ["legal"],
  "filters": {"jurisdictions": ["CN"]}
}
```

- `verticals`：可选、去重列表；首期唯一值为 `legal`。省略时保持当前 API 行为。
- `verticals=["legal"]`：强制纳入 FY；通用 Web provider 仍运行，以保留独立网页出处和 FY 故障降级。
- `source_types=["web"]` 不等价于 `verticals=["legal"]`，因此不会改变已有客户端请求。
- 若显式请求 `legal` 且 `jurisdictions` 明确不含 `CN`/`CN-*`，在传输层返回 422，避免把中国法律库误用于其他法域。

### 5.2 自动路由

新增纯规则 `detect_legal(query)`，只用于补充 route，不能改变 query 文本。建议高精度词典包括：

- 明确法律词：`法律`、`法规`、`法条`、`司法解释`、`条例`、`判决`、`案例`、`起诉`、`合同`、`借条`、`欠条`、`抚养权`、`劳动仲裁`、`行政处罚`；
- 中国法制实体：`民法典`、`最高法`、`人民法院`、`刑法`、`公司法`、`劳动法`；
- 不路由的消歧词：技术专利检索、法学院名称、游戏/影视中的“法律”等。

仅当 query 以中文为主、未声明非中国法域、且 `FY_LEGAL_AUTO_ROUTE_ENABLED=true` 时自动启用。每次自动命中须写入内部 `RetrievalBoundary` 的 route reason；公开响应可显示通用的 `LEGAL_VERTICAL_AUTO_ROUTED` 限制/诊断代码，但不泄露内部供应商名称。

### 5.3 Registry 与 plan 的最小演进

为 `SourceDescriptor` 增加兼容默认的 `route_tags: frozenset[str] = {"general"}`；FY descriptor 声明 `{"legal"}`。`SearchPlan` 增加冻结的 `verticals` 和 `route_reasons`，`QueryPlanner` 只把以下 Web source 写入 `active_provider_names`：

- `general`：始终保持现有规则；
- `legal`：仅显式选择或 `detect_legal` 命中时加入。

这比在 `RecallCoordinator` 根据 provider 名称写 if/else 更可扩展；后续金融、招聘或医疗垂直库可复用同一机制。

## 6. FY 调用与认证设计

### 6.1 配置

新增以下部署级 Settings，均为 secret-safe（`repr=False`）：

| 配置 | 说明 | 默认 |
|---|---|---|
| `FY_LEGAL_ENABLED` | 是否注册 FY 法律 provider | `false` |
| `FY_API_BASE_URL` | FY HTTP 基地址，无尾部 `/` | 宁夏使用 `https://api.cjbdi.com` |
| `FY_APP_ID` | `FYDN-OP-AppID` | 无 |
| `FY_API_KEY` | 签名盐与 token 获取的 `appKey` | 无 |
| `FY_API_SECRET` | token 获取的 `appSecret` | 无 |
| `FY_LEGAL_AUTO_ROUTE_ENABLED` | 启用高精度自动路由 | `false` |
| `FY_LEGAL_MAX_CANDIDATES` | FY 复合来源最大候选数 | `12` |
| `FY_LEGAL_TOKEN_REFRESH_SKEW_SECONDS` | token 提前刷新窗口 | `300` |
| `FY_LEGAL_TOKEN_STORE` | `memory` 或 `redis` | `memory`（仅开发） |

`FY_LAW_MCP_TOKEN` 不参与本 REST 检索接入；它是另一个 MCP OAuth 资源的令牌，不能当作 `FYDN-OP-AccessToken` 使用。

`FY_LEGAL_ENABLED=true` 时要求 `FY_APP_ID`、`FY_API_KEY`、`FY_API_SECRET` 全部存在。`FY_API_BASE_URL` 必须是 HTTPS，host 必须在受控 allowlist 内，TLS 校验不得关闭。

### 6.2 accessKey 生命周期

`FyAccessTokenProvider` 负责：

1. 通过 `POST {base}/auth/api/token/getAccess?appKey=…&appSecret=…` 获取 `data.accessKey` 和 `expires`。
2. 在内存或 Redis 中保存 accessKey 与单调过期时间；刷新点为 `expires - refresh_skew`。
3. 对同一 `app_id` 做进程内 singleflight；生产多实例使用 Redis 分布式锁和共享 token record。
4. 当业务调用返回 `5000` 时，在持锁条件下只刷新一次并重试一次；其余错误不得靠无限刷新掩盖。

多实例生产环境若没有共享 token store，不允许开启 FY provider：接口文档说明重复获取会使旧 token 失效，多个实例各自刷新会相互造成 5000。

### 6.3 请求签名

每次请求生成 UUID `FYDN-OP-RequestId`。对 JSON body 递归按 key 升序、UTF-8、无空格序列化，计算：

```text
canonical_body = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
FYDN-OP-Sign = md5(utf8(canonical_body + FY_API_KEY)).hexdigest()
```

必填 headers：

```text
FYDN-OP-RequestId: <uuid>
FYDN-OP-Sign: <md5>
FYDN-OP-AccessToken: <cached accessKey>
FYDN-OP-AppID: <FY_APP_ID>
Content-Type: application/json
```

签名输入、headers 和 token 只能在内存中存在。日志只记录 request id、endpoint 类别、HTTP 状态、FY 业务码、耗时和 query hash。

## 7. 检索编排与结果映射

### 7.1 endpoint 选择

`FyLegalSearchProvider` 在一个 provider deadline 内执行如下计划：

| 阶段 | 调用 | 触发条件 | 默认候选上限 | 结果类别 |
|---|---|---|---:|---|
| P0 | `law_view` | 所有 legal 请求 | 5 | `statute` |
| P0 | `similar_question` | 所有 legal 请求 | 4 | `qa` |
| P1 | `t2wechat_view` | P0 少于 3 条，或 query 含案例/实务/解读 | 3 | `practice` |
| P1 | `web_view` | P0 少于 3 条，或 query 含观点/争议/舆情 | 3 | `opinion` |

P0 的两个调用可并发；P1 仅在剩余 provider deadline 足够时启动。总数受 `min(request.candidate_budget, FY_LEGAL_MAX_CANDIDATES)` 约束。每个 endpoint 必须接收同一 normalized query；不得用法律模型悄悄扩写 query。

`flux/38` 是生成式 SSE 咨询，可能持续数十秒且不返回独立检索条目。首期不由 provider 调用；后续如需要，应新增显式的 `POST /legal/consult` 或 Research 内部 answer synthesis，并返回单独的 `generated_answer`、模型版本、流式状态和非法律意见声明。

### 7.2 统一 SearchResult 映射

| FY 字段 | `SearchResult` 映射 | 说明 |
|---|---|---|
| `title` | `title` | 原样保留 |
| `content` | `content` 与 `snippet` | `snippet` 截断至现有 provider 约定；保留完整受许可的 provider extract |
| `url` | `url` | 无 URL 时保持空，不伪造官方链接 |
| endpoint 类别 | `raw.fy_knowledge_type` | `statute/qa/practice/opinion`，内部 provenance 使用 |
| endpoint 路径与返回状态 | `raw`/batch diagnostics | 仅内部，不公开供应商/密钥细节 |

`law_view` 的测试结果可没有稳定 URL。此时 Evidence 保留法规标题和 passage，但 `access.is_open=false`；不能把 FY 的门户首页或猜测的法规链接填为 locator。后续只有取得法规版本、发文字号、条次和可访问的官方 URL 后，才能升级为稳定法规 locator。

### 7.3 排序、去重与配额

- 所有 FY 项与普通 Web 项先经过现有 URL 去重和 Web ranking；同 URL 的项目沿用现有“更完整内容优先”规则。
- 保留 `knowledge_type` 至 `RetrievedDocument`/`RankedDocument` 的内部 metadata，不能在 DTO 转换时丢失。
- `verticals=["legal"]` 且存在 statute 候选时，最终 `limit` 内至少保留一条 `statute`；其余按现有综合相关性排序。此配额必须在 ranking/selection 策略中实现，不通过人工提高 provider 原始分数实现。
- `qa`、`practice`、`opinion` 不是彼此独立的事实来源。Research 的身份归并按 URL、标题/正文 hash 和发布者处理，不能仅因来自不同 FY endpoint 而增加独立来源计数。

## 8. 过滤、可信边界与公开呈现

### 8.1 过滤

FY 已测试接口不支持调用方传入的日期、语言或辖区过滤。适配器声明 `snippet`/`full_content` 能力，不声明 `time_range_filter`、`language_filter` 或可变 `jurisdiction_filter`。

- 中国法域是来源固有边界，不等于已精确应用 `filters.jurisdictions`。
- 显式非中国法域请求不调 FY；未指定法域但中文法律意图可自动路由。
- `published_from`、`published_to`、`languages` 必须在 `filter_execution` 中报告为 unsupported/not applicable，不能回显为已应用。

### 8.2 质量与法律安全

| 结果类别 | `/search` 质量定位 | 禁止的表述 |
|---|---|---|
| `statute` | 受许可的法规检索摘录；在缺少版本/条次/官方 URL 时为 `limited` | “已定位到现行有效的完整法条” |
| `qa` | 发现型问答 | “官方法律意见” |
| `practice` | 实务/案例线索 | “具有普遍约束力的裁判规则” |
| `opinion` | 网络观点和背景 | “法律依据” |
| `flux/38` 输出 | 生成性咨询，不是 evidence | “检索到的法规原文” |

`/research` 可以把 FY 作为发现源和反证线索，但涉及法律有效性、辖区、具体条款和法律结论时，必须继续取得可定位的原文或官方来源；沿用现有 Trust Layer 的“不得替代专业法律判断”边界。

## 9. 失败、限流与可观测性

| FY 业务码 | 分类 | 行为 |
|---:|---|---|
| `1000` / `1001` | 成功 / 空结果 | 正常返回；空结果遵从 descriptor 的 `count_empty_as_used` 策略 |
| `5000` | token 无效/过期 | singleflight 刷新后仅重试一次 |
| `5001` | IP 白名单 | 不重试；告警 `FY_IP_NOT_ALLOWLISTED` |
| `5002` / `5006` / `5007` / `5008` | 请求、签名、解密、时钟实现错误 | 不重试；高优先级工程告警 |
| `5003` / `5004` | 配额不足 / 授权过期 | 不重试；熔断并通知运营 |
| `5005` / `5108` / `5109` / `5110` | 上游失败、熔断或限流 | 交给既有 ResilienceManager 有界重试、退避和熔断 |
| 网络超时、TLS/JSON 错误 | 外部依赖失败 | 使用 `external_http_error`，不泄露 URL query 或凭据 |

新增指标（全部只带 query hash）：

- `fy_legal_requests_total{endpoint,outcome}`、`fy_legal_latency_ms`、`fy_legal_results_total{knowledge_type}`；
- `fy_legal_token_refresh_total{outcome}`、`fy_legal_token_age_seconds`、`fy_legal_sign_failures_total`；
- `fy_legal_auto_route_total`、`fy_legal_fallback_total{reason}`、`fy_legal_quota_or_auth_total{code}`。

`/health` 只报告 `fy_legal` 的配置/注册状态、token 是否在有效期和 circuit snapshot，不主动调用 FY 或暴露 token、端点细节和余额。

## 10. 实现切分

| 改动 | 责任 |
|---|---|
| `src/config.py` | FY Settings、成组校验、URL 校验、默认与 secret-safe repr |
| `src/infrastructure/fy_legal_auth.py` | `FyAccessTokenProvider`、singleflight、可注入 clock/token store、签名 canonicalization |
| `src/providers/fy_legal.py` | FY HTTP adapter、endpoint 计划、DTO 归一化、真实过滤/诊断、错误码映射 |
| `src/application/commands.py`、`interfaces/schemas.py`、`mcp_server.py` | 可选 `verticals` 输入与 REST/MCP 一致映射 |
| `src/application/ports/retrieval.py`、`source_registry.py`、`domain/search.py` | 兼容默认的 route tag 与 plan vertical metadata |
| `src/l0.py`、`application/query_planner.py` | 纯 `detect_legal`、显式优先和 provider 选择 |
| `src/application/recall.py`、`ranking/` | provider deadline 内的候选配额、legal statute diversity 选择 |
| `src/bootstrap.py` | 仅在配置完整时装配 FY adapter/token provider；统一 HTTP Session/lifecycle |
| `tests/`、`eval/` | 单元、契约、路由和宁夏 E2E smoke 覆盖 |

不要在 `api.py`、`SearchService` 或 `EvidenceAssembler` 中植入 FY endpoint/凭据/签名分支；这些层只消费既有 Command、RetrievalBatch 和 Evidence 模型。

## 11. 验证与上线

### 11.1 自动化测试

1. **认证与签名单测**：固定 JSON（含中文、嵌套数组、浮点数）与 FY 示例的 MD5 一致；secret 不出现在异常/`repr`。
2. **token 生命周期测试**：缓存命中、提前刷新、并发 singleflight、5000 刷新一次、Redis 锁丢失与超时。
3. **provider contract**：四类 endpoint 的成功、空结果、URL 缺失、坏 JSON、每个 FY 业务码、HTTP 超时和 deadline；断言真实 filters/limitations。
4. **路由契约**：普通查询零 FY 调用；显式 legal 必调；高精度法律 query 可自动路由；非 CN 法域显式请求被拒绝。
5. **排序与 evidence**：法规配额、跨 endpoint 去重、无 URL 法规不伪造 locator、`qa/practice/opinion` 质量边界不被提升。
6. **REST/MCP 往返**：同一 Command 的 active source、failure code、evidence 次序一致；旧请求不增加 `verticals` 仍保持旧输出语义。

### 11.2 宁夏 E2E smoke

在白名单已配置、`FY_API_BASE_URL=https://api.cjbdi.com` 的部署机上，每次发布只执行有限的只读检索：

- `law_view`：验证法规条目；
- `similar_question`：验证 QA 条目；
- `t2wechat_view` 与 `web_view`：仅在 dedicated smoke job；
- `flux/38`：仅验证首个 SSE event，不同步等待完整模型回答。

测试不打印 accessKey、AppID、API key、签名或完整用户 query。生产 health check 不执行这些付费请求。

### 11.3 灰度与回滚

1. 先合入 adapter、配置和 mock contract，`FY_LEGAL_ENABLED=false`。
2. 在宁夏单实例以 `verticals=["legal"]` 内部流量灰度，比较 P50/P95、法规命中率、失败码、成本与通用 Web 基线。
3. 开启 `FY_LEGAL_AUTO_ROUTE_ENABLED`，仅对高精度中文法律 query 灰度；监控误路由率。
4. 多实例部署前完成 Redis token store 和分布式锁验证。
5. 任意故障时将 `FY_LEGAL_ENABLED=false`，Registry 不注册来源，现有 Web 搜索不改代码即可回滚。

## 12. 验收标准

- 显式 legal 请求在 FY 可用时返回至少一个标明法规/问答类别的 FY evidence；普通请求不触发 FY。
- FY 单点超时、空结果、限流或鉴权失败不阻断其它 Web provider，也不泄露供应商或 secret。
- `law_view` 无 URL 的结果不会被标记为 open/official locator；Research 不据此生成未经核验的法律结论。
- 同一 accessKey 在有效期内被复用；并发和多副本场景不会相互使 token 失效。
- REST 与 MCP 的 `verticals=["legal"]` 结果、filters、failure 和缓存语义一致。
- 宁夏 E2E 验证在可用 443 基地址成功；如果运维要求 8443，则必须先通过网络与白名单验收，而不是在应用代码里隐式绕过。
