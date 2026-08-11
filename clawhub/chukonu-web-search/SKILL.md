---
name: chukonu-web-search
description: "通过 Chukonu remote MCP 的 search 与 research 获取网页、学术、专利和中国法律法规证据。用于需要外部、实时、可引用信息的搜索任务，以及法律法规检索、事实核验、反证检索、PDF 全文深读、覆盖评估或持久化多轮研究；依据结构化 evidence、retrieval_assessment 与 research dossier 作答。"
---

# Chukonu Search + Research

通过 Chukonu remote MCP 调用公开的 `search` 和 `research` 工具。无需本地二进制、API key 或静态 token；不要调用内部工具、上游服务或内部端口。法律法规检索是服务内部接入的 FY MCP provider，客户端不要单独配置它、传入其凭据或尝试调用其工具。

## 一次性接入

使用 `https://search.houdutech.cn/web/mcp/` streamable-http 端点。该端点通过标准 OAuth 2.0 保护，采用动态客户端注册、授权码、PKCE(S256) 和 refresh token，scope 为 `search:read`。让 MCP host 自动发现并注册，不要手工填写 client ID 或 client secret。

OpenClaw：

```bash
openclaw mcp add chukonu-web-search \
  --url https://search.houdutech.cn/web/mcp/ \
  --transport streamable-http \
  --auth oauth
openclaw mcp login chukonu-web-search
```

Claude Code：

```bash
claude mcp add chukonu-web-search https://search.houdutech.cn/web/mcp/ \
  --scope user --transport http
# 然后在会话中：/mcp → chukonu-web-search → Authenticate
```

Codex：

```bash
codex mcp add chukonu-web-search --url https://search.houdutech.cn/web/mcp/
codex mcp login chukonu-web-search
codex mcp list
```

在 Codex TUI 中用 `/mcp` 查看连接状态。若 OAuth 浏览器与 Codex 不在同一主机，先为 Codex 配置可回调的 `mcp_oauth_callback_url`，再登录；不要输出或持久化授权码、回调 URL、access token 或 refresh token。

不要为该服务器配置静态 `Authorization` 头或 `CHUKONU_SEARCH_API_TOKEN`。静态 Authorization 头会使 host 禁用 OAuth 回退，并可能导致 `401 invalid_token`。把 OAuth token 完全交给 host 保管，不要在日志、回答、示例或错误信息中泄露。

## 选择工具与来源

- 用 `search` 处理秒级发现、法规定位和获取后续研究所需的 `research_seed.search_id`。
- 用 `research` 处理关键主张核验、反证、跨来源冲突、全文定位或证据覆盖评估。必须先 `search`，再用返回的 `search_id` 启动任务。
- `source_types` 只接受 `web`、`academic`、`patent` 和 `legal`。省略时服务自动路由；法律意图可在法律 provider 启用时与通用网页并行召回。
- 用 `source_types: ["legal"]` 只检索中国法律法规；用 `source_types: ["legal", "web"]` 同时获取法条和通用网页背景。
- 对法律问题尽量写明法名和条号，例如 `《中华人民共和国民法典》第1084条`；仅把原始法条证据用于法律效力或条文结论。
- 不要把搜索相关性分数解释为事实置信度，也不要把检索结果或本技能当作法律意见。

## `search`

### 请求

最小调用：

```json
{
  "query": "固态电池硫化物电解质近五年的关键路线"
}
```

只检索现行有效的中国法规：

```json
{
  "query": "《中华人民共和国民法典》第1084条",
  "limit": 10,
  "source_types": ["legal"],
  "filters": {
    "legal_status": "现行有效",
    "jurisdictions": ["CN"]
  }
}
```

混合检索法条和网页解读：

```json
{
  "query": "劳动合同解除的法定情形和近期解读",
  "source_types": ["legal", "web"],
  "filters": {
    "legal_status": "现行有效",
    "languages": ["zh"]
  }
}
```

遵守以下边界：

- `query` 必填；保持简洁、具体。`limit` 是最终全局返回数，范围为 1–20。
- `filters` 支持 `published_from`、`published_to`、`languages`、`jurisdictions` 和 `legal_status`。`legal_status` 仅接受 `尚未生效`、`现行有效`、`已被修改`、`失效`、`待核实`。
- 日期、语言和法域过滤是否真正生效取决于来源。法律 provider 是中国法规来源，当前实际执行的是 `legal_status`；非中国法域和其他未支持过滤器会在响应中标明。不要仅因请求中带有过滤器就声称过滤已应用。
- 不要发送请求级模型、重排、PDF 或 trust 开关；未知字段会被拒绝。

### 解读搜索结果

按以下顺序检查响应：

1. 检查 `failures[]`，确认是否有来源或阶段失败。
2. 检查 `result_set.counts_by_stage` 中 `web`、`academic`、`patent`、`legal` 的 `recalled/ranked/assembled/selected` 计数，定位候选丢失阶段。
3. 检查 `retrieval_assessment.status` 与 `retrieval_assessment.gaps[]`，判断证据是否足够。
4. 检查 `query.filter_execution`，确认每项过滤器实际 `applied`、`unsupported` 或 `not_applicable` 的状态。
5. 对法律证据读取 `type: "legal"`、`passage`、`legal.law_type`、`legal.status`、`legal.department`、`legal.directory` 与 `legal.item`。引用时保留法规标题和条号。
6. 法律证据可能没有公开 URL，且 `access.is_open` 为 `false`。不要臆造官方链接、发布日期、法规效力或全文定位；原文被截断时遵守 `diagnostics` 中的限制。
7. 仅用 `evidence[].scores.relevance` 排序，不将其视为可信度或法律效力判断。

`status` 只表示搜索执行是否完整，不代表证据充分性。`research_seed.search_id` 指向服务端保存的不可变 evidence 与检索边界快照；不要构造、修改或回传该快照。

## `research`

`research` 是持久化研究任务的统一生命周期工具，支持 `start`、`get`、`feedback` 和 `cancel`。

### 启动任务

从搜索响应取得 `research_seed.search_id` 后调用：

```json
{
  "operation": "start",
  "search_id": "srch_...",
  "idempotency_key": "agent-run-20260811-001",
  "profile": "technology_validation",
  "depth": "standard",
  "objective": {
    "question": "硫化物电解质的关键路线是什么，哪些已形成专利布局？",
    "claims": [
      {
        "text": "硫化物电解质已形成较完整的专利布局",
        "importance": "key"
      }
    ],
    "required_features": ["离子电导率", "界面稳定性", "制备方法"]
  }
}
```

- 必须提供 `search_id` 和全局 `idempotency_key`。为同一个逻辑请求的重试复用同一个 key；只有请求实质改变时才换 key。
- 选择 `profile`：`literature_review`、`technology_validation`（默认）、`prior_art_landscape` 或 `technology_landscape`；选择 `depth`：`quick`、`standard` 或 `deep`。
- 把问题写入 `objective.question`，已知待核验陈述写入 `objective.claims`，必须覆盖的维度写入 `objective.required_features`。
- 仅在要收紧预设上限时提供 `budget`；不要用它扩大预设预算。

### 读取、补充与取消

读取任务：

```json
{
  "operation": "get",
  "research_id": "rsch_...",
  "detail": "full"
}
```

- 当 `state` 为 `queued` 或 `running` 时，等待响应中的 `retry_after_ms` 再读取；不要高频轮询。
- 仅在 `needs_input` 时提交 `feedback`。先读取最新任务，再以当前 `task_revision` 提交真实的 `answers` 或 `note`；不要替用户虚构答案。
- 取消任务时使用最新的 `task_revision`。终态包括 `completed`、`partial`、`needs_input`、`failed` 与 `cancelled`。
- `state="completed"` 只表示流程正常停止。用 `dossier.assessment.overall` 判断结论充分性，并从 finding 的 evidence ID 解引用 `dossier.evidence_index`；同时检查 `dossier.coverage.gaps` 和顶层 `stop`。

## 作答规则

- 让每个关键结论对应具体 evidence，并保留 finding、evidence 和 locator 的关系。区分搜索片段、法规条文、PDF 原文和研究结论。
- 法律问题先说明适用法域、检索时点、法条状态和证据缺口；将法条内容、事实适用和结论明确分开。对于个案、争议或跨法域问题，提示用户咨询合格的法律专业人士。
- 学术结论优先使用可定位的原始论文；专利结论优先使用专利文献，并区分申请、公开、授权和未知状态。
- 对时效性问题检查证据日期、`legal.status` 和 `filter_execution`。日期陈旧、状态待核实、过滤未执行或证据缺少公开链接时，明确披露限制。
- 当 `retrieval_assessment.status` 为 `limited`，有实质 `failures[]`，或法律/研究证据为 `partial`、`insufficient`、`conflicted`、`needs_expert_review` 时，不要给出无保留的确定性结论。
- 不要生成或声称接口提供单一 `trust_score`，不要臆造来源、法规元数据、引文、申请人、发明人、日期、许可证或 URL。

## 错误处理

- `401 invalid_token` 或未授权：重新执行 `openclaw mcp login chukonu-web-search`、`codex mcp login chukonu-web-search`，或在 Claude Code 中通过 `/mcp → chukonu-web-search → Authenticate` 重新完成 OAuth；同时检查并移除误配的静态 Authorization 头。
- `search` 参数被拒绝：移除未知字段，确认 `limit`、`source_types`、日期和 `legal_status` 的格式。
- 法律 provider 不可用：阅读 `failures[]` 和 `retrieval_assessment.gaps[]`；只有在普通网页证据仍覆盖主张时才以明确限制继续作答。
- `research start` 幂等冲突：若逻辑请求未变则恢复原请求并复用原 key；若请求变了才用新 key。
- `research` 返回 `needs_input`：读取 `input_request`，向用户取得答案后用最新 revision 提交。返回 `failed`、`partial` 或 `cancelled`：读取 `failures[]`、`stop` 和 coverage gaps，并说明已完成部分与限制。
