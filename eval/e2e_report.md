# E2E 评测报告 (n=6, k=10)

- mode: `inprocess SearchEngine.search()`
- judge: off (--no-judge)
- latency_budget_p95: 8000 ms

## 总览
| Final | BundleQuality | Route | RequiredEvidence | P95 latency | LatencyScore |
|-------|---------------|-------|----------------|-------------|--------------|
| N/A | N/A | 1.000 | 1.000 | 3921 ms | 1.000 |

## 路由明细
| Metric | Score |
|--------|-------|
| academic_route_acc | 1.000 |
| patent_route_acc | 1.000 |
| timely_route_acc | 1.000 |

## 失败样本
| Query | Type | Route | Blocks | Judge | Reason |
|-------|------|-------|--------|-------|--------|
| - | - | - | - | - | - |

## 全量样本
| Query | Type | ms | Web | Academic | Patent | Route | Judge |
|-------|------|----|-----|----------|--------|-------|-------|
| 三星堆遗址在哪个省 | factual | 3921 | 10 | 0 | 0 | 1.00 | N/A |
| 光合作用的基本过程是什么 | factual | 3312 | 10 | 0 | 0 | 1.00 | N/A |
| 珠穆朗玛峰的海拔高度是多少 | factual | 3651 | 10 | 0 | 0 | 1.00 | N/A |
| 中国四大发明分别是什么 | factual | 3145 | 10 | 0 | 0 | 1.00 | N/A |
| 2026年人工智能领域有哪些最新进展 | timely | 3676 | 10 | 0 | 0 | 1.00 | N/A |
| 最近一届诺贝尔物理学奖授予了哪些研究 | timely | 2905 | 4 | 0 | 0 | 1.00 | N/A |
