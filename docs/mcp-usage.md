# MCP 使用说明：Search + Research

MCP 与 REST 只暴露两个业务能力：

- `search`：秒级、单轮、轻量发现；
- `research`：持久化、多轮、可轮询的可信研究任务。

陈述校验和 PDF 全文读取仍是服务内部阶段，不再单独暴露 `verify_claims`、`get_pdf_text`。

## 1. `search`

最小调用：

```json
{
  "query": "固态电池硫化物电解质近五年的关键路线"
}
```

完整调用：

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

规则：

- `limit` 范围为 1–20，表示最终全局返回数；
- `source_types` 不传时自动路由，传入后只检索指定类型；
- 未知字段会被拒绝；不支持请求级模型、重排、PDF 或 trust 开关；
- `status` 只表示执行是否完整，证据是否可用看 `retrieval_assessment`；
- `evidence[].scores.relevance` 只用于排序，不是事实置信度；
- `research_seed.search_id` 指向服务端不可变 evidence + boundary 快照。

Agent 应先检查：

1. `failures[]`；
2. `retrieval_assessment.status` 与 `gaps[]`；
3. `query.filter_execution` 是否真正应用了所需过滤器；
4. `evidence[].quality`；
5. 需要事实核验、反证或全文时，转入 `research`。

## 2. `research`

### 2.1 启动

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

最小业务输入是 `search_id`，但 `operation=start` 必须提供全局幂等键。相同 key 和等价请求返回同一任务；相同 key 对应不同请求会失败。

`profile`：

- `literature_review`
- `technology_validation`（默认）
- `prior_art_landscape`
- `technology_landscape`

`depth`：`quick | standard | deep`。显式 budget 只能收紧预设上限。

### 2.2 读取

```json
{
  "operation": "get",
  "research_id": "rsch_...",
  "detail": "full"
}
```

任务状态为 `queued/running` 时按 `retry_after_ms` 轮询。终态包括 `completed/partial/needs_input/failed/cancelled`。

注意：`state=completed` 表示研究流程正常停止，不表示结论已证实。结论充分性必须读取 `dossier.assessment.overall`：

- `sufficient`
- `sufficient_with_limitations`
- `insufficient`
- `conflicted`
- `needs_expert_review`

每个 finding 的引用可在 `dossier.evidence_index` 按 evidence ID 解引用。剩余问题位于 `dossier.coverage.gaps`，停止原因位于顶层 `stop`。接口不返回单一 `trust_score`。

### 2.3 Feedback 与取消

```json
{
  "operation": "feedback",
  "research_id": "rsch_...",
  "task_revision": 3,
  "answers": {"target": "只关注量产路线"}
}
```

Feedback 只接受 `needs_input` 状态，并用 `task_revision` 做并发保护。

```json
{
  "operation": "cancel",
  "research_id": "rsch_...",
  "task_revision": 3
}
```

## 3. Python 客户端示例

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with streamable_http_client("http://localhost:8000/mcp") as streams:
        async with ClientSession(*streams[:2]) as session:
            await session.initialize()
            search = await session.call_tool("search", {
                "query": "sulfide solid electrolyte interface stability",
                "limit": 10,
                "source_types": ["academic", "patent", "web"],
            })
            # 从 search JSON 中读取 research_seed.search_id。
            task = await session.call_tool("research", {
                "operation": "start",
                "search_id": "srch_...",
                "idempotency_key": "example-001",
                "depth": "standard",
            })


asyncio.run(main())
```

鉴权仍使用 HTTP `Authorization: Bearer <token>` 或 `X-API-Key`；本接口不建立租户身份模型。
