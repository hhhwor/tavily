---
name: chukonu-web-search
version: 0.2.0
description: "网络搜索：通过 Chukonu remote MCP server 获取面向 Agent 的网页、学术与专利检索证据。自然语言查询，返回证据优先的结构化结果（evidence[]、answerability 可答性、partial_failure 部分失败诊断），可选学术/专利意图开关与重排。只读、OAuth 登录。"
tags: [web, search, research, academic, patent, mcp, evidence, oauth]
metadata:
  transport: streamable-http
  requires:
    mcp:
      - name: chukonu-web-search
        url: "https://search.houdutech.cn/web/mcp/"
        transport: streamable-http
        auth: oauth
  setup:
    # OpenClaw
    - "openclaw mcp add chukonu-web-search --url https://search.houdutech.cn/web/mcp/ --transport streamable-http --auth oauth"
    - "openclaw mcp login chukonu-web-search"
    # Claude Code（本次实践；切勿配置静态 Authorization 头，否则会禁用 OAuth）
    - "claude mcp add chukonu-web-search https://search.houdutech.cn/web/mcp/ --scope user --transport http"
    - "在 Claude Code 中：/mcp → 选择 chukonu-web-search → Authenticate 完成浏览器 OAuth"
---

# chukonu-web-search

通过 Chukonu 的 **remote MCP server** 检索网页、学术与专利证据。本 skill 不调用本地二进制，也**不需要 API key / 静态 token** —— 它把 host（OpenClaw 或 Claude Code）接到托管的 `chukonu-web-search` MCP server，**OAuth 登录、只读检索**。

## 一次性接入（首次使用前）

端点：`https://search.houdutech.cn/web/mcp/`（streamable-http）。该端点通过标准 OAuth 2.0 保护：动态客户端注册 + 授权码 + PKCE(S256) + refresh_token，scope `search:read`。host 会自动发现并注册，无需手工填 client_id/secret。

OpenClaw：

```bash
openclaw mcp add chukonu-web-search \
  --url https://search.houdutech.cn/web/mcp/ \
  --transport streamable-http \
  --auth oauth
openclaw mcp login chukonu-web-search     # 弹浏览器完成 OAuth，token 由 host 保管
```

Claude Code：

```bash
claude mcp add chukonu-web-search https://search.houdutech.cn/web/mcp/ \
  --scope user --transport http
# 然后在会话中：/mcp → chukonu-web-search → Authenticate（浏览器 OAuth）
```

> 关键：**不要**为该服务器配置 `Authorization` 头或 `CHUKONU_SEARCH_API_TOKEN`。一旦设置了静态 Authorization 头，host 会禁用 OAuth 回退，导致 `401 invalid_token`。鉴权完全交给 OAuth 流程。

接入并授权后会出现两个只读 tool：`search` 和 `get_pdf_text`。

## 稳定能力

使用 `search` 获取网页、学术与专利证据；需要学术论文全文原文时，先让 `search` 抽取 PDF 正文，再用 `get_pdf_text` 按游标续读。`get_pdf_text` 只读取已经抽取的缓存，不会自行下载或解析 PDF。不要直接调用内部上游服务——对外入口只有 `https://search.houdutech.cn/web/mcp/`。

## 何时使用本 skill

- 需要外部、实时、可引用的信息（新闻、事实核查、技术细节、时效性问题）——不要凭记忆作答
- 学术检索：论文、研究、DOI、arXiv、OpenAlex、引文、文献综述（用 `include_academic=true`）
- 专利检索：专利、发明、申请人/权利人、发明人、IPC/CPC、公开号、FTO（用 `include_patent=true`）

## 工具

### `search` —— 检索入口

用简洁的自然语言查询调用：

```
search(query="问题或搜索关键词", top_k=10)
```

参数：

| 参数 | 说明 |
|---|---|
| `query` | 自然语言查询或关键词（必填） |
| `top_k` | 直接作答用 5-10；研究/对比类用 10-20 |
| `include_academic` | 论文/DOI/arXiv/OpenAlex/引文/综述类任务用 `true`；学术证据会成噪声时用 `false`；否则 `null` |
| `include_patent` | 专利/发明/申请人/发明人/IPC-CPC/公开号/FTO 类任务用 `true`；否则 `null` |
| `rerank` | 默认 `null`（服务默认重排）；仅对排序质量要求不高的低延迟探索用 `false` |
| `include_pdf_text` | 需要学术 PDF 正文时用 `true`；默认 `false` |
| `pdf_text_mode` | `cached` 只读已有缓存；`sync` 允许本次搜索下载并解析 PDF；否则 `null` |
| `pdf_max_results` | 最多为多少篇学术结果补充 PDF 正文；默认由服务决定 |
| `pdf_max_chars_per_result` | 每篇 PDF 首次返回的正文字符上限；默认由服务决定 |

示例：

```
search(query="固态电池 电解质 界面阻抗", top_k=15, include_academic=true, include_patent=true)
```

需要论文全文时：

```
search(query="固态电池 电解质 界面阻抗", top_k=10,
       include_academic=true, include_pdf_text=true, pdf_text_mode="sync")
```

### `get_pdf_text` —— 续读学术 PDF 正文

仅在 `search(include_pdf_text=true)` 返回的 academic evidence 含有 `citation.work_id`，且 `access.next_cursor` 非空时调用：

```
get_pdf_text(work_id="W123", cursor="<access.next_cursor>", max_chars=8000)
```

参数：

| 参数 | 说明 |
|---|---|
| `work_id` | OpenAlex work ID（必填）；必须从 academic evidence 的 `citation.work_id` 取得 |
| `cursor` | 首次续读传 `access.next_cursor`，之后传上一次响应的 `next_cursor`；默认 `null` |
| `max_chars` | 本次最多返回的字符数，默认 `8000`，服务端限制为 1–30000 |

响应包含：

- `status`：本次读取状态。
- `text`：本页 PDF 正文。
- `page_from` / `page_to` / `chunk_index`：正文定位信息。
- `returned_chars`：本次返回的字符数。
- `next_cursor`：下一页游标；为 `null` 时没有后续正文。
- `partial`：是否仍有后续正文。
- `error_code` / `error_message`：失败诊断。

续读时沿用服务返回的游标，不要自行构造或修改；当 `next_cursor=null` 或现有正文已足够回答时停止。

## 响应结构

响应以证据为先：

- `evidence[]`：按相关性排序的 `web`、`academic`、`patent` 混合证据。
- `answerability.status`：`answerable` / `partial` / `not_answerable`。
- `answerability.gaps[]`：明确列出缺失的证据或质量缺口。
- `partial_failure`：任一提供方、重排或增强子任务失败时为 `true`。
- `failures[]`：机器可读的失败详情。
- `meta.counts`：按证据类型统计的数量。

每个证据条目：

- `passage.text`：可用的证据文本。
- `citation`：label、authors、year、DOI、work_id 或 publication_number 等引用元数据。
- `patent`：仅当 `type="patent"` 时的专利结构化元数据（公开号/申请号、申请人、发明人、IPC/CPC、国家、状态、family_id、日期、patent_type、citation_count）。
- `scores`：relevance、rank、rerank、authority、confidence 等信号。
- `access`：开放程度、许可、OA PDF URL、PDF 状态。
- `diagnostics`：该条证据的告警、部分失败状态、失败代码。

## 决策准则

- 用户给主题词/技术描述 → `search(query=…)`，按意图叠加 `include_academic` / `include_patent`
- 需要更多召回做研究/对比 → 提高 `top_k`（10-20），而不是反复换词重搜
- 需要论文原文且摘要不足 → `search(include_academic=true, include_pdf_text=true, pdf_text_mode="sync")`
- academic evidence 的 `access.next_cursor` 非空且仍需更多原文 → 用其 `citation.work_id` 和游标调用 `get_pdf_text`
- 先查 `partial_failure`、`failures[]`、`answerability.gaps[]` 再组织答案

## 回答规则

- `answerability.status="not_answerable"`：不要给自信的最终答案；说明缺什么，重新细化检索或询问用户是否继续。
- `partial_failure=true`：使用已返回的有效证据；仅当失败来源会实质影响可信度时才提及它。
- 学术类：优先带 DOI、venue、year、authors 与开放获取状态的 `academic` 证据；用 `citation.label`、DOI、URL 引用。
- 专利类：优先 `patent` 证据，用 `patent.publication_number`、`patent.applicant`、`patent.inventor`、`patent.ipc_main`/`patent.cpc_main` 及公开/申请日期；不要在返回的 `patent.status` 之外推断法律状态。
- PDF 正文：优先用 `page_from` / `page_to` 等定位信息引用；不要把摘要、搜索片段与 PDF 原文混为一谈。
- 时效性问题：检查 `published_date`，优先较新证据；日期陈旧或缺失时说明不确定性。
- 所有论断都要落到 `passage.text` 上；不要臆造来源、元数据、论断、引文、申请人、发明人、日期、许可或专利状态。绝不在日志/回答/示例/错误信息中泄露 token。

## 错误处理

| code | 应对 |
|---|---|
| `UNAUTHORIZED` / `401 invalid_token` | 重新 OAuth 授权：`openclaw mcp login chukonu-web-search`，或 Claude Code `/mcp → chukonu-web-search → Authenticate`。检查有没有误配静态 Authorization 头 |
| `QUOTA_EXCEEDED` | 已达配额；告知用户重置时间或稍后重试 |
| `INVALID_ARGUMENT` | 核对参数（`top_k` 范围、`include_academic`/`include_patent` 取值）后重试 |
| `BACKEND_UNAVAILABLE` | 上游暂时不可用，稍后重试 |
| `WORK_ID_MISSING` | 必须从 academic evidence 的 `citation.work_id` 传入有效 work ID |
| `PDF_TEXT_TIMEOUT` | PDF 正文读取超时；稍后重试或基于已经返回的证据作答 |
| `PDF_TEXT_READ_FAILED` | 检查 `work_id`、`cursor` 和 PDF 抽取状态；必要时重新用 `search(include_pdf_text=true, pdf_text_mode="sync")` 初始化正文缓存 |
