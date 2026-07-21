---
name: chukonu-web-search
description: "通过 Chukonu remote MCP 的 search 与 research 获取网页、学术和专利证据。用于需要外部、实时、可引用信息的搜索任务，以及需要事实核验、反证检索、PDF 全文深读、覆盖评估或持久化多轮研究的任务；依据结构化 evidence、retrieval_assessment 与 research dossier 作答。"
metadata:
  version: "0.3.0"
  tags: [web, search, research, academic, patent, mcp, evidence, oauth]
  transport: streamable-http
  requires:
    mcp:
      - name: chukonu-web-search
        url: "https://search.houdutech.cn/web/mcp/"
        transport: streamable-http
        auth: oauth
  setup:
    - "openclaw mcp add chukonu-web-search --url https://search.houdutech.cn/web/mcp/ --transport streamable-http --auth oauth"
    - "openclaw mcp login chukonu-web-search"
    - "claude mcp add chukonu-web-search https://search.houdutech.cn/web/mcp/ --scope user --transport http"
    - "在 Claude Code 中：/mcp → 选择 chukonu-web-search → Authenticate 完成浏览器 OAuth"
---

# Chukonu Search + Research

通过 Chukonu remote MCP 获取网页、学术和专利证据。本 skill 不调用本地二进制，也不需要 API key 或静态 token。只使用公开的 `search` 与 `research` 两个业务工具；陈述校验和 PDF 全文读取是 `research` 的内部阶段，不要调用 `verify_claims`、`get_pdf_text` 或内部上游服务。

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

不要为该服务器配置静态 `Authorization` 头或 `CHUKONU_SEARCH_API_TOKEN`。静态 Authorization 头会使 host 禁用 OAuth 回退，并可能导致 `401 invalid_token`。把 OAuth token 完全交给 host 保管，不要在日志、回答、示例或错误信息中泄露。

## 选择工具

- 用 `search` 完成秒级、单轮、轻量发现，以及获取后续研究所需的 `search_id`。
- 当任务需要事实核验、反证、PDF 全文、证据覆盖评估或可信的多轮研究时，先调用 `search`，再用其 `research_seed.search_id` 启动 `research`。
- 不要把搜索结果的相关性排序当作事实置信度。高风险结论或证据存在明显缺口时，优先转入 `research`。

## `search`

### 请求

最小调用：

```json
{
  "query": "固态电池硫化物电解质近五年的关键路线"
}
```

需要约束来源或过滤范围时：

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

遵守以下约束：

- `query` 必填；保持查询简洁、具体。
- `limit` 为最终全局返回数，范围是 1–20。
- `source_types` 仅接受 `web`、`academic`、`patent`。省略时让服务自动路由；传入后只检索指定类型。
- `filters` 支持 `published_from`、`published_to`、`languages`、`jurisdictions`。
- 不要发送请求级模型、重排、PDF 或 trust 开关。未知字段会被拒绝。

### 检查结果

按以下顺序检查搜索响应：

1. 读取 `failures[]`，确认是否有来源或阶段失败。
2. 读取 `retrieval_assessment.status` 和 `retrieval_assessment.gaps[]`，判断证据是否可用及缺口。
3. 读取 `query.filter_execution`，确认所需过滤器是否真正应用；不要仅根据请求参数假设过滤成功。
4. 检查每条 `evidence[].quality`，优先使用质量更高、可定位、可引用的证据。
5. 仅把 `evidence[].scores.relevance` 用于排序，不要将其解释为事实置信度。

`status` 只表示搜索执行是否完整，不能代替证据充分性判断。`research_seed.search_id` 指向服务端保存的不可变 evidence 与检索边界快照；不要自行构造、修改或让客户端回传该快照。

## `research`

`research` 是持久化研究任务的统一生命周期工具，支持 `start`、`get`、`feedback` 和 `cancel`。

### 启动任务

从搜索响应取得 `research_seed.search_id` 后调用：

```json
{
  "operation": "start",
  "search_id": "srch_...",
  "idempotency_key": "agent-run-20260717-001",
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

启动时：

- 必须提供 `search_id` 和全局 `idempotency_key`。
- 为同一个逻辑请求的重试复用同一个幂等键。相同 key 与等价请求返回同一任务；相同 key 用于不同请求会失败。
- 根据目标选择 `profile`：`literature_review`、`technology_validation`（默认）、`prior_art_landscape` 或 `technology_landscape`。
- 根据研究强度选择 `depth`：`quick`、`standard` 或 `deep`。
- 仅在需要收紧预设上限时显式传入 `budget`；不要用它扩大预设预算。
- 用 `objective.question` 表达问题；已知待核验陈述放入 `objective.claims`；必须覆盖的维度放入 `objective.required_features`。

### 读取与轮询

```json
{
  "operation": "get",
  "research_id": "rsch_...",
  "detail": "full"
}
```

当 `state` 为 `queued` 或 `running` 时，等待响应给出的 `retry_after_ms` 后再次读取，不要高频轮询。终态包括 `completed`、`partial`、`needs_input`、`failed` 和 `cancelled`。

### 补充输入

仅当任务处于 `needs_input` 时提交 feedback。先读取最新任务，使用其当前 `task_revision`：

```json
{
  "operation": "feedback",
  "research_id": "rsch_...",
  "task_revision": 3,
  "answers": {
    "target": "只关注量产路线"
  }
}
```

需要用户选择时，先把 `input_request` 中的问题转述给用户；不要替用户虚构答案。`task_revision` 用于并发保护，发生版本冲突时重新读取任务后再决定是否提交。

### 取消任务

```json
{
  "operation": "cancel",
  "research_id": "rsch_...",
  "task_revision": 3
}
```

取消前使用最新的 `task_revision`，避免基于过期状态操作任务。

## 解读研究结果

- `state="completed"` 只表示研究流程正常停止，不表示结论已被证实。
- 用 `dossier.assessment.overall` 判断结论充分性：`sufficient`、`sufficient_with_limitations`、`insufficient`、`conflicted` 或 `needs_expert_review`。
- 从每个 finding 的 evidence 引用 ID 出发，在 `dossier.evidence_index` 中解引用原始证据。
- 从 `dossier.coverage.gaps` 读取尚未覆盖的问题，从顶层 `stop` 读取停止原因。
- 不要生成或声称接口返回了单一 `trust_score`。

## 作答规则

- 让关键论断落到具体 evidence；不要臆造来源、元数据、引文、申请人、发明人、日期、许可或专利状态。
- 学术结论优先使用可定位的原始论文证据；专利结论优先使用专利文献，并区分申请、公开、授权及未知状态。
- 对时效性问题检查证据日期；日期陈旧、缺失或过滤未落实时明确说明限制。
- 搜索为 `limited` 或存在实质性 `failures[]` 时，可以使用仍有效的证据，但必须披露会影响结论的缺口；需要核验时转入 `research`。
- 研究为 `partial`、`insufficient`、`conflicted` 或 `needs_expert_review` 时，不要输出无保留的确定性结论。
- 引用研究结果时同时保留 finding、evidence 和 locator 之间的对应关系，不要把摘要、搜索片段和 PDF 原文混为一谈。

## 错误与边界处理

- `401 invalid_token` 或未授权：重新执行 `openclaw mcp login chukonu-web-search`，或在 Claude Code 中通过 `/mcp → chukonu-web-search → Authenticate` 重新完成 OAuth；同时检查并移除误配的静态 Authorization 头。
- `search` 参数被拒绝：移除未知字段，并检查 `limit`、来源类型、日期和过滤器格式。
- `research start` 幂等冲突：如果逻辑请求未变，恢复原请求并复用原 key；只有请求确实改变时才使用新 key。
- `research` 返回 `needs_input`：读取 `input_request`，获得真实答案后用最新 revision 提交 feedback。
- `research` 返回 `failed`、`partial` 或 `cancelled`：读取 `failures[]`、`stop` 和 coverage gaps，向用户说明已完成部分及限制。

远程 MCP 的鉴权由 host 按 OAuth 流程完成。服务不建立租户身份模型；始终把 OAuth 凭据视为秘密。
