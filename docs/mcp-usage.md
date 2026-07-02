# chukonu-web-search MCP 使用文档

面向 AI Agent / LLM 的多源检索 MCP server。把本项目的搜索引擎(腾讯 + 百度 + SerpAPI 聚合,可选 OpenAlex 学术 + houdutech 专利 ES,跨源去重 + cross-encoder 重排)包成一个 MCP 工具,与 REST 服务**同进程、同端口**对外提供。

- 传输:**Streamable HTTP**(无状态 + JSON 响应)
- 端点:`<base>/mcp`(例:`http://localhost:8000/mcp`)
- 服务名(serverInfo.name):`chukonu-web-search`
- 工具:`search`(目前唯一工具)

---

## 1. 工具:`search`

搜索网络并返回 LLM-ready 的结构化结果。当回答需要**外部或最新信息**(新闻、事实核查、技术细节、时效问题、学术论文、专利)时调用,而不要凭记忆作答。学术与专利意图会自动识别,也可强制开关。

### 入参

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | string | (必填) | 检索词 / 自然语言问题 |
| `top_k` | int | `10` | 每块返回条数 |
| `include_academic` | bool? | `null` | 学术检索(OpenAlex):`null`=按意图自动,`true`=强制开,`false`=强制关 |
| `include_patent` | bool? | `null` | 专利检索(ES):同上 |
| `rerank` | bool? | `null` | `null`=服务端默认,`true`=开 cross-encoder 重排(质量更高,慢数秒),`false`=走 RRF 快路径 |

### 返回(结构化 JSON)

```jsonc
{
  "query": "...",
  "recency": "month",            // 时效 bucket(自动识别),无则 null
  "web": [                        // 网页结果
    {"title", "url", "content", "score", "source", "date"}
  ],
  "academic": [                   // 学术论文(命中学术意图时;否则空)
    {"title", "url", "oa_url", "oa_landing_url", "oa_pdf_url",
     "authors", "year", "venue", "citations", "doi",
     "is_oa", "oa_status", "content"}
  ],
  "patents": [                    // 专利(命中专利意图时;否则空)
    {"title", "url", "publication_number", "applicant", "inventor",
     "country", "classification", "application_date", "patent_type", "content"}
  ],
  "meta": {"providers_used", "reranker", "elapsed_ms",
           "counts": {"web", "academic", "patents"}}
}
```

> 正文 `content` 每条截断到约 600 字以省 token。专利无原生网页,`url` 用 Google Patents 落地页。
>
> 学术结果里 `url` 是论文主页面(DOI/OpenAlex),`oa_landing_url` 是 OA 落地页,`oa_pdf_url` 是 OA PDF 直链。`oa_url` 保留为兼容字段,语义是“泛化 OA 链接”(优先 landing,退化到 pdf)。

---

## 2. 鉴权

服务端配了 `API_AUTH_TOKEN` 后,`/mcp`(和 `/search`)强制校验,二选一:

- `Authorization: Bearer <token>`(MCP 客户端首选)
- `X-API-Key: <token>`

不带或错误 → 401(MCP 握手会失败)。token 在服务器 `.env` 的 `API_AUTH_TOKEN`,用 `grep ^API_AUTH_TOKEN= .env` 取;**不要外泄、不要提交**。`<base>/health` 的 `auth` 字段反映是否开启。

---

## 3. 接入方式

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

## 4. `<base>` 怎么填(本地 vs 外网)

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

## 5. 启动服务端(运维参考)

```bash
cd /home/ec2-user/tavily
scripts/serve.sh -d            # 后台启动(自动用 .venv311 / Python 3.11;MCP 需 ≥3.10)
curl -s localhost:8000/health  # auth / mcp / providers 状态
# 停止:kill <pid>(启动信息里给出)
```

相关环境变量(`.env`):`API_AUTH_TOKEN`(鉴权)、`MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` / `MCP_DNS_REBINDING_PROTECTION`(反代/隧道放行)、`RERANK_ENABLED`(质量↑/延迟↑)。详见 [tech-route-summary.md](./tech-route-summary.md) §5/§7。

---

## 6. 注意事项

- **token 是唯一网关**:URL 不是秘密,token 才是;泄漏即失守。轮换:改 `.env` 的 `API_AUTH_TOKEN` 后重启。
- **别裸绑明文公网**:对外要么走 SSH 隧道(已加密),要么走 cloudflared / nginx+TLS(HTTPS);明文 HTTP 暴露公网会泄露 token。
- **延迟**:开重排(`rerank=true` 或服务端默认开)单次约 +2~5s;低延迟可走 RRF 快路径。
- **专利当事人字段**:中国授权(CN-B)文献的 `applicant`/`inventor` 曾大面积缺失,已随上游索引升级到 `epo_docdb_v2_20260620`(读别名 `epo_docdb_read`)修复——当事人现为 object `{original/docdb/docdba}`,中文原文名也可检索(详见 [patent-es-cn-b-missing-applicant-bug.md](./patent-es-cn-b-missing-applicant-bug.md))。
