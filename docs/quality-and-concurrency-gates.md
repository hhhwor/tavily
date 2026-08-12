# Agent Search 质量门禁与并发基准

> 稳定性改造总览见 [agent-search-stability-summary.md](./agent-search-stability-summary.md)。

质量 Golden Gate 用于阻断可复现的排序质量退化。并发程序只保留为按需基准，不进入默认测试、合并或发布门禁。

## 一键执行

要求 Python 3.11：

```bash
.venv311/bin/python -m eval.run_stability_gates
```

该命令执行 Search 排序与 Research Dossier 两组质量门禁。也可以分别执行：

```bash
.venv311/bin/python -m eval.quality_golden_gate
.venv311/bin/python -m eval.research_quality_gate
```

按需观察并发性能时手工执行：

```bash
.venv311/bin/python -m eval.concurrency_gate
```

质量门禁失败时退出码非 0。并发基准只输出观测结果和 advisory，不因 20/50 档位未达到参考值而返回失败。报告均为本机产物，不提交 Git。

## 质量 Golden Gate

固定语料 `eval/golden/quality_corpus.json` 包含 Web、Academic、Patent 各 3 条查询，每条 5 个候选及 0–3 级相关性。运行器通过三个生产领域排序策略计算 NDCG@5、Recall@5、Precision@5 和 MRR，并逐领域及整体与 `quality_baseline.json` 比较。

当前规则：

- corpus SHA-256 必须与基线一致，避免语料被静默替换；
- 任一领域的任一指标相对审定基线绝对下降超过 0.02，即阻断；
- 更新语料或有意调整排序策略时，必须显式执行以下命令并评审 corpus、排序明细和指标变化：

```bash
.venv311/bin/python -m eval.quality_golden_gate --update-baseline
```

该门槛使用确定性 token-overlap scorer，目的是锁定领域排序策略和特征组合的行为；线上模型效果仍由带真实 provider/模型的完整评测覆盖。

Research 门禁固定使用 `eval/golden/research_quality_corpus.json` 中 50 条、覆盖四个 profile 的标注场景，检查 claim support precision、locator validity、identity、counterevidence、冲突披露、gap disclosure 和无依据事实句率。安全指标使用零容忍阈值；调整 corpus 时需显式执行并评审：

```bash
.venv311/bin/python -m eval.research_quality_gate --update-baseline
```

## 可选 20/50 并发基准

`eval/concurrency_gate.py` 使用真实的 QueryPlanner → RecallCoordinator → RankingService → EvidenceAssembler → TrustAnnotator → SQLite Seed Store 链路，只把公网 provider 和 scorer 替换为带确定延迟的受控实现。

默认负载：

| 并发 | 请求数 | Provider 延迟 | Scorer 延迟 | 首次瞬时失败 | Deadline |
|---:|---:|---:|---:|---:|---:|
| 20 | 40 | 20 ms | 5 ms | 每 10 个请求 1 次 | 1500 ms |
| 50 | 100 | 20 ms | 5 ms | 每 10 个请求 1 次 | 1500 ms |

`eval/golden/concurrency_thresholds.json` 仅保留历史参考值，基准会观察：

- success、complete、usable、重试恢复率均为 100%；
- exception 和 Deadline failure 为 0；
- 20/50 并发 P95 分别不超过 1000/1500 ms；
- 吞吐不低于 20 requests/s；
- 召回并行度至少为 2 且不超过 16、排序并行度在 2–4，证明负载确有重叠且绝不突破各自线程池上限；
- resilience retry 计数必须与注入的瞬时失败数一致。

该基准不在 pytest、合并或发布路径中执行，也不代表第三方 provider 的容量结论。需要容量结论时，应在目标网络、目标实例规格和真实连接池配置下做独立 soak test。
