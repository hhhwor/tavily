# Agent 场景对比报告 (n=2, k=8)

场景: 技术尽调/R&D 情报 agent。主分来自 agent 基于搜索结果写出的最终回答。

## 总览
| Metric | full_agent | baidu_only |
|--------|------------|------------|
| avg_latency_ms | 16006 | 1334 |
| p95_latency_ms | 19127 | 1373 |
| avg_web_evidence | 8.0 | 6.5 |
| avg_academic_evidence | 4.0 | 0.0 |
| avg_patent_evidence | 8.0 | 0.0 |

## 场景明细
| ID | Domain | Evidence full(web/acad/pat) | Evidence baidu | Answer winner | Answer scores | Reason |
|----|--------|-----------------------------|----------------|---------------|---------------|--------|
| sodium_battery_cathode | battery | 8/8/8 | 5 | N/A | N/A |  |
| solid_state_sulfide_electrolyte | battery | 8/0/8 | 8 | N/A | N/A |  |
