# Tool Agent E2E 报告 (n=1)

- mcp_url: `http://127.0.0.1:8000/mcp`
- model: `claude-haiku-4-5-20251001`
- judge: off

## 总览
| Metric | Value |
|--------|-------|
| tool_call_rate | 1.000 |
| avg_tool_calls | 4.00 |
| avg_required_source_coverage | 1.000 |
| total_support_audit_flags | 0 |
| gap_disclosure_rate | 1.000 |
| p95_elapsed_ms | 53082 |

## 场景明细
| ID | Domain | Tools(search/pdf) | Evidence(web/acad/pat) | Coverage | Partial | AuditFlags | Judge |
|----|--------|-------------------|------------------------|----------|---------|------------|-------|
| sodium_battery_cathode | battery | 4(4/0) | 47/30/20 | 1.00 | 0 | 0 | N/A |
