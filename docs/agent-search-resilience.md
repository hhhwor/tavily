# Agent Search 重试、熔断与降级矩阵

> 稳定性改造总览见 [agent-search-stability-summary.md](./agent-search-stability-summary.md)。

## 1. 重试规则

- 仅重试 `ExternalServiceError(recoverable=true)`；鉴权失败、请求参数错误和其他
  明确不可恢复错误不重试。
- 默认最多执行 2 次（首次调用 + 1 次重试）。
- 采用指数退避和 jitter；上游返回 `Retry-After` 时取两者较大值。
- 如果退避会越过请求 deadline，则立即停止重试并保留最后一个真实错误。
- Provider、查询改写和重排共享同一个策略实现，但使用独立 dependency key 和
  熔断状态。

## 2. 熔断规则

- 默认连续 3 个“已耗尽重试的可恢复失败”后打开熔断器。
- 默认 30 秒后进入 half-open，只允许一个探针请求。
- 探针成功即关闭并清零连续失败；探针失败则重新打开。
- 不可恢复错误和请求自身 deadline 不计入依赖熔断。
- 熔断拒绝返回 `code=CIRCUIT_OPEN` 和 `retry_after_ms`，由服务端负责探测恢复，
  Agent 不应紧密循环重试。

## 3. 公开降级矩阵

每个 search failure 都返回 `degradation.action / impact / retry_owner`。

| Stage | 典型失败 | 服务端降级动作 | 影响 | retry_owner |
|---|---|---|---|---|
| `query_rewrite` | timeout / 429 / circuit open | `use_original_query` | quality | server |
| `academic_query_rewrite` | timeout / 429 / circuit open | `use_original_query` | quality | server |
| `routing` | Provider 未启用 | `continue_available_sources` | coverage | none |
| `provider_search` | timeout / 5xx / circuit open | `continue_available_sources` | coverage | server |
| `provider_search` | 请求 deadline 耗尽 | `continue_available_sources` | coverage | caller |
| `rerank` | timeout / 5xx / circuit open | `use_unreranked_results` | quality | server |
| `seed_store` | SQLite 暂不可用 | `omit_research_seed` | feature | caller |
| `pdf_enrichment` | 下载或解析失败 | `use_abstract_or_metadata` | quality | server |
| `claim_entailment` | 外部模型失败 | `use_rule_verification` | quality | server |

`retry_owner` 语义：

- `server`：服务端重试/熔断恢复负责，Agent 应使用当前降级结果。
- `caller`：只有调用方改变 deadline 或确实需要缺失能力时才应重试。
- `none`：重试不能解决问题，需要配置或输入变化。

最终是否可以回答仍以 `retrieval_assessment.status` 和 `gaps[]` 为准，不能只根据
单个 failure 判断。
