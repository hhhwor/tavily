# chukonu-web-search MCP 使用文档

面向 AI Agent / LLM 的多源检索 MCP server。把本项目的搜索引擎(腾讯 + 百度 + SerpAPI 聚合,可选 OpenAlex 学术 + houdutech 专利 ES,跨源去重 + cross-encoder 重排)包成一个 MCP 工具,与 REST 服务**同进程、同端口**对外提供。

- 传输:**Streamable HTTP**(无状态 + JSON 响应)
- 端点:`<base>/mcp`(例:`http://localhost:8000/mcp`)
- 服务名(serverInfo.name):`chukonu-web-search`
- 工具:`search`、`verify_claims`、`get_pdf_text`

---

## 1. 工具:`search`

搜索网络并返回 LLM-ready 的结构化结果。当回答需要**外部或最新信息**(新闻、事实核查、技术细节、时效问题、学术论文、专利)时调用,而不要凭记忆作答。学术与专利意图会自动识别,也可强制开关。

### 入参

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | string | (必填) | 检索词 / 自然语言问题 |
| `top_k` | int | `10` | 各检索分支重排保留上限;最终按相关性混排到 `evidence[]` |
| `include_academic` | bool? | `null` | 学术检索(OpenAlex):`null`=按意图自动,`true`=强制开,`false`=强制关 |
| `include_patent` | bool? | `null` | 专利检索(ES):同上 |
| `rerank` | bool? | `null` | `null`=服务端默认,`true`=开 cross-encoder 重排(质量更高,慢数秒),`false`=走 RRF 快路径 |
| `include_pdf_text` | bool | `false` | 是否对重排后的前几篇学术结果同步补 PDF 正文 |
| `pdf_text_mode` | string? | `null` | `cached`=只读缓存,`sync`=允许本次下载解析 |
| `pdf_max_results` | int? | `null` | 本次最多富化几篇 PDF |
| `pdf_max_chars_per_result` | int? | `null` | 每篇 PDF 抽取正文返回字符上限 |
| `trust_mode` | string | `annotate` | `annotate`=补 provenance/locator/quality/检索边界;`off`=旧 evidence 路径 |

### 返回(结构化 JSON)

```jsonc
{
  "query": "...",
  "recency": "month",            // 时效 bucket(自动识别),无则 null
  "trust_mode": "annotate",
  "search_boundary": {
    "source_snapshot": {"openalex_local": "service-index:unspecified"},
    "query_time": "2026-07-15T00:00:00Z",
    "languages": ["zh"], "jurisdictions": [], "license_scope": ["cc-by"],
    "max_rounds": 1, "max_candidates": 30, "deadline_ms": null,
    "limitations": ["SINGLE_ROUND_SEARCH", "NO_GLOBAL_DEADLINE"]
  },
  "partial_failure": true,        // 任一子任务失败即为 true; evidence 仍可能可用
  "failures": [
    {"stage": "provider_search", "source": "openalex_local",
     "type": "academic", "code": "PROVIDER_SEARCH_FAILED",
     "message": "...", "recoverable": true}
  ],
  "answerability": {
    "status": "partial",          // answerable / partial / not_answerable
    "confidence": "medium",       // high / medium / low / none
    "gaps": [
      {"code": "NO_ACADEMIC_EVIDENCE", "severity": "warning",
       "message": "查询需要学术证据,但未返回论文证据。", "type": "academic"}
    ]
  },
  "evidence": [                   // web / academic / patent 按相关性混排
    {
      "id": "academic:W123:pdf:0",
      "result_id": "academic:W123",
      "type": "academic",
      "source": "openalex_local",
      "title": "...",
      "url": "...",
      "published_date": "2026",
      "passage": {"text": "...", "snippet_type": "pdf_text"},
      "citation": {"label": "Smith et al., 2026", "doi": "...", "work_id": "W123"},
      "patent": null,
      "scores": {"relevance": 0.91, "rerank_score": 0.91, "source_rank": 0},
      "access": {"is_open": true, "license": "cc-by", "oa_pdf_url": "...", "pdf_status": "ready", "next_cursor": null},
      "diagnostics": {"warnings": [], "partial": false, "failure_code": null},
      "provenance": {"canonical_url": "...", "retrieved_via": "openalex_local", "content_origin": "fulltext", "document_id": "W123", "version_id": "...", "retrieved_at": "..."},
      "locator": {"document_id": "W123", "version_id": "...", "page_from": 4, "page_to": 5, "chunk_index": 0},
      "quality": {"level": "citable", "is_original": true, "has_stable_locator": true, "can_support_key_claim": true, "reasons": []}
    }
  ],
  "meta": {"providers_used", "reranker", "elapsed_ms",
           "counts": {"web", "academic", "patent"}}
}
```

> 证据正文在 `passage.text` 中,每条裁剪到约 1800 字以省 token。专利无原生网页,`url` 用 Google Patents 落地页。
>
> 学术证据的 `citation` 携带作者/年份/期刊/DOI/OpenAlex work_id;开放获取和 PDF 状态放在 `access` 中。`access.next_cursor` 非空表示 PDF 正文还有后续内容可继续读取。
>
> 专利证据的 `citation` 只保留引用用字段,完整结构化元数据放在 `patent` 子对象中:`publication_number/application_number/applicant/inventor/ipc_main/cpc_main/country/status/family_id/application_date/publication_date/patent_type/citation_count`。
>
> Agent 应先看 `partial_failure` 和 `answerability.gaps`。`partial_failure=true` 不代表完全失败,而是至少一路 provider / rerank / PDF 富化失败;可用证据仍在 `evidence[]`。`answerability.status=not_answerable` 时不应直接生成确定性回答。
>
> `trust_mode=annotate` 是 Phase 0 证据分级，不是陈述级事实验证。`provider_extract` 通常为 `limited`，论文/专利摘要和搜索 snippet 为 `discovery_only`；只有带稳定原文 locator 的全文片段才可能为 `citable`。

---

## 2. 工具:`verify_claims`

把候选事实陈述与 `search` 返回的 evidence 做陈述级校验。Phase 1 只校验传入证据，不自动发起补充检索或反证检索。

### 入参

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | string | 必填 | 产生候选陈述的原始问题 |
| `claims` | object[] | 必填 | `[{id,text,claim_type?,importance?,subject?,predicate?,value?,unit?,time_scope?,jurisdiction?}]` |
| `evidence` | object[] | 必填 | `search` 返回的 `evidence[]` |
| `profile` | string | `general` | `general/news/scientific/patent/legal/financial/product` |
| `search_boundary` | object? | `null` | 建议原样传入 `search.search_boundary` |

返回 `assessments[]`，每条包含：

- `status`：`supported/conflicted/insufficient/inference/needs_expert_review`；
- `relations[]`：每条 evidence 的 `supports/contradicts/mentions/unclear/irrelevant` 判断、原文、locator 和一致性检查；
- `support_refs/conflict_refs/mention_refs`；
- 独立支持数、一手来源数、gaps、follow-up queries 和人工复核标记；
- 顶层 `trust_assessment` 汇总证据覆盖率和无依据陈述率。

```jsonc
{
  "query": "材料循环寿命是多少",
  "profile": "scientific",
  "assessments": [{
    "claim": {"id": "c1", "text": "材料循环寿命达到 1000 次", "importance": "key"},
    "status": "supported",
    "confidence": "medium",
    "support_refs": ["academic:W123:pdf:0"],
    "conflict_refs": [],
    "gaps": ["COUNTEREVIDENCE_NOT_SEARCHED"],
    "followup_queries": ["材料循环寿命达到 1000 次 争议 反例"]
  }],
  "trust_assessment": {
    "status": "supported", "claims_total": 1, "supported_claims": 1,
    "evidence_coverage_rate": 1.0, "unsupported_statement_rate": 0.0,
    "policy_version": "trust-phase1-v1"
  },
  "failures": []
}
```

> 只有 `quality.can_support_key_claim=true` 且一致性检查通过的证据才能计为合格支持。摘要、snippet、provider extract 或无稳定 locator 的 PDF 即使包含相同文字，也返回 `insufficient`。Phase 1 尚未主动反证，因此 supported 最高为 `medium`。

校验后端由环境变量控制：`TRUST_VERIFY_BACKEND=auto|rules|siliconflow`（默认 `auto`，有 SiliconFlow key 时使用模型）、`TRUST_VERIFY_MODEL`、`TRUST_VERIFY_TIMEOUT`、`TRUST_VERIFY_MAX_CLAIMS` 和 `TRUST_VERIFY_MAX_EVIDENCE`。模型调用失败会记录 `ENTAILMENT_BACKEND_FAILED`，并降级到不以语义相似度产生支持的保守规则。

---

## 3. 工具:`get_pdf_text`

当 `search(include_pdf_text=true)` 返回的 academic evidence 带有 `access.next_cursor` 时,Agent 可继续读取后续 PDF 正文:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `work_id` | string | (必填) | 从 evidence 的 `citation.work_id` 取值 |
| `cursor` | string? | `null` | 从 evidence 的 `access.next_cursor` 或上次 `get_pdf_text.next_cursor` 取值 |
| `max_chars` | int | `8000` | 本次最多返回字符数,服务端裁剪到 1–30000 |

返回:

```jsonc
{
  "work_id": "W123",
  "status": "ready",
  "chunk_index": 2,
  "page_from": 4,
  "page_to": 5,
  "text": "...",
  "returned_chars": 8000,
  "next_cursor": "...",
  "partial": true,
  "error_code": null,
  "error_message": null
}
```

> `get_pdf_text` 只读已抽取缓存,不触发下载解析。若还没有抽取,先用 `search` 打开 `include_pdf_text=true` 并让同步解析主链路完成。

---

## 4. 鉴权

服务端配了 `API_AUTH_TOKEN` 后,`/mcp`(和 `/search`)强制校验,二选一:

- `Authorization: Bearer <token>`(MCP 客户端首选)
- `X-API-Key: <token>`

不带或错误 → 401(MCP 握手会失败)。token 在服务器 `.env` 的 `API_AUTH_TOKEN`,用 `grep ^API_AUTH_TOKEN= .env` 取;**不要外泄、不要提交**。`<base>/health` 的 `auth` 字段反映是否开启。

---

## 5. 接入方式

### Claude Code

```bash
claude mcp add chukonu-web-search --transport http \
  <base>/mcp \
  --header "Authorization: Bearer <token>"
# 未开鉴权时去掉 --header。工具在对话里显示为 mcp__chukonu-web-search__search
```

### Claude Desktop / 仅支持 stdio 的客户端

用 `mcp-remote` 桥接(`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "chukonu-web-search": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "<base>/mcp",
               "--header", "Authorization: Bearer <token>"]
    }
  }
}
```

### MCP Inspector(调试)

```bash
npx @modelcontextprotocol/inspector
# 连 <base>/mcp,在 Headers 里填 Authorization: Bearer <token>
```

### Python 客户端(自检 / 编程调用)

```python
import anyio, json
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def main():
    headers = {"Authorization": "Bearer <token>"}
    async with streamablehttp_client("<base>/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("search", {"query": "什么是 RAG", "top_k": 3,
                                               "include_patent": False})
            print(json.loads(res.content[0].text)["meta"]["counts"])

anyio.run(main)
```

---

## 6. `<base>` 怎么填(本地 vs 外网)

引擎跑在 EC2 上,服务监听 `:8000`。`<base>` 取决于你怎么连:

### A. 本地 / 自己用 —— SSH 隧道(推荐,加密、零暴露)

```bash
ssh -i <密钥.pem> -N -L 8000:localhost:8000 ec2-user@<EC2>
# <base> = http://localhost:8000
```

### B. 外网 —— Cloudflare 隧道(HTTPS,自带 TLS)

服务器上起快速隧道,得到一个临时 HTTPS 域名:

```bash
cloudflared tunnel --no-autoupdate --url http://localhost:8000
# 输出形如 https://<random>.trycloudflare.com,即 <base>
```

⚠️ 用外网域名时,需在 `.env` 把该域名加入 MCP 的 Host 白名单(DNS rebinding 防护默认开启),否则 MCP 握手返回 **421**:

```bash
echo 'MCP_ALLOWED_HOSTS=<random>.trycloudflare.com' >> .env   # 重启服务生效
```

> 快速隧道域名每次重启都会变,变了要同步更新 `MCP_ALLOWED_HOSTS` 并重启。要长期稳定域名用 Cloudflare **命名隧道**(需账号 + 域名),Host 固定、白名单一次设定。

---

## 7. 启动服务端(运维参考)

```bash
cd /home/ec2-user/tavily
scripts/serve.sh -d            # 后台启动(自动用 .venv311 / Python 3.11;MCP 需 ≥3.10)
curl -s localhost:8000/health  # auth / mcp / providers 状态
# 停止:kill <pid>(启动信息里给出)
```

相关环境变量(`.env`):`API_AUTH_TOKEN`(鉴权)、`MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` / `MCP_DNS_REBINDING_PROTECTION`(反代/隧道放行)、`RERANK_ENABLED`(质量↑/延迟↑)。详见 [tech-route-summary.md](./tech-route-summary.md) §5/§7。

---

## 8. 注意事项

- **token 是唯一网关**:URL 不是秘密,token 才是;泄漏即失守。轮换:改 `.env` 的 `API_AUTH_TOKEN` 后重启。
- **别裸绑明文公网**:对外要么走 SSH 隧道(已加密),要么走 cloudflared / nginx+TLS(HTTPS);明文 HTTP 暴露公网会泄露 token。
- **延迟**:开重排(`rerank=true` 或服务端默认开)单次约 +2~5s;低延迟可走 RRF 快路径。
- **专利当事人字段**:中国授权(CN-B)文献的 `applicant`/`inventor` 曾大面积缺失,已随上游索引升级到 `epo_docdb_v2_20260620`(读别名 `epo_docdb_read`)修复——当事人现为 object `{original/docdb/docdba}`,中文原文名也可检索(详见 [patent-es-cn-b-missing-applicant-bug.md](./patent-es-cn-b-missing-applicant-bug.md))。
