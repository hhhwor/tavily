"""Secure 55-item Doubao-only BrowseComp-ZH Pilot runner.

This is the A1 search-backend isolation track. It reuses the B2 planner,
finalizer, page reader, model, judge, sample, and budgets, while replacing the
four-source Chukonu backend with a direct Doubao provider adapter.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from eval.browsecomp_zh_eval import (
    B2_PLANNER_SYSTEM,
    FINALIZER_SCHEMA,
    FINALIZER_SYSTEM,
    JUDGE_SCHEMA,
    JUDGE_SYSTEM,
    OPEN_URL_TOOL,
    SEARCH_TOOL,
    AnswerFinalizer,
    B2Agent,
    ModelClient,
    PageReader,
    SingleSourceSearchClient,
    _atomic_json,
    _judge_self_test,
    _leak_filter_self_test,
    _search_events_are_single_source,
    _utc_now,
)
from eval.browsecomp_zh_pilot import (
    DEFAULT_MODEL,
    EXPECTED_DATASET_SHA256,
    EXPECTED_REPO_COMMIT,
    EXPECTED_TOPIC_COUNTS,
    _freeze_hash,
    _run_system,
    _secure_write,
    _system_summary,
    load_encrypted_dataset,
    select_pilot,
)
from src.config import Settings
from src.providers.doubao import DoubaoSearchProvider


DEFAULT_SUMMARY = Path("eval/browsecomp_zh_doubao_pilot_summary.json")
DEFAULT_REPORT = Path("eval/browsecomp_zh_doubao_pilot_report.md")


class RetryingSingleSourceSearchClient:
    """Apply a frozen, deadline-aware retry policy around one provider."""

    def __init__(
        self,
        inner: SingleSourceSearchClient,
        *,
        timeout: float,
        max_attempts: int = 2,
        backoff_seconds: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        self.inner = inner
        self.backend_id = inner.backend_id
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._sleeper = sleeper
        self.total_upstream_attempts = 0
        self.total_upstream_successes = 0
        self.failure_codes: Counter[str] = Counter()

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempts": self.total_upstream_attempts,
            "successes": self.total_upstream_successes,
            "failure_codes": dict(self.failure_codes),
        }

    @staticmethod
    def delta(
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        codes = set(before["failure_codes"]) | set(after["failure_codes"])
        return {
            "attempts": after["attempts"] - before["attempts"],
            "successes": after["successes"] - before["successes"],
            "failure_codes": {
                code: after["failure_codes"].get(code, 0)
                - before["failure_codes"].get(code, 0)
                for code in sorted(codes)
                if after["failure_codes"].get(code, 0)
                - before["failure_codes"].get(code, 0)
            },
        }

    def search(
        self,
        query: str,
        *,
        limit: int,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], int]:
        started = time.perf_counter()
        total_timeout = min(self.timeout, timeout) if timeout else self.timeout
        deadline = time.monotonic() + total_timeout
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.total_upstream_attempts += 1
            try:
                response, _ = self.inner.search(
                    query,
                    limit=limit,
                    timeout=remaining,
                )
                self.total_upstream_successes += 1
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                return response, elapsed_ms
            except Exception as exc:
                last_error = exc
                code = str(getattr(exc, "code", "")) or type(exc).__name__
                self.failure_codes[code] += 1
                recoverable = bool(getattr(exc, "recoverable", True))
                if not recoverable or attempt + 1 >= self.max_attempts:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                retry_after = float(
                    getattr(exc, "retry_after_seconds", 0.0) or 0.0
                )
                pause = min(
                    max(self.backoff_seconds, retry_after),
                    remaining,
                )
                if pause > 0:
                    self._sleeper(pause)
        if last_error is not None:
            raise last_error
        raise TimeoutError("doubao single-source search deadline exhausted")

    def close(self) -> None:
        self.inner.close()


def _failure_kind(item: dict[str, Any]) -> str:
    error = str(item.get("error") or "")
    if "HTTP 429" in error:
        return "model_http_429"
    if "ReadTimeout" in error:
        return "model_read_timeout"
    if "total call deadline" in error:
        return "model_total_deadline"
    if "BudgetExceeded" in error:
        return "budget_exhausted"
    return error.split(":", 1)[0] or "unknown"


def _baseline_comparison(
    summary: dict[str, Any],
    baseline_path: Path,
) -> dict[str, Any]:
    if not baseline_path.exists():
        return {"available": False, "reason": "baseline_summary_not_found"}
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    matched = all([
        baseline.get("dataset_sha256") == summary["dataset_sha256"],
        baseline.get("seed") == summary["seed"],
        baseline.get("sample_size") == summary["sample_size"],
    ])
    if not matched:
        return {"available": False, "reason": "baseline_protocol_mismatch"}
    systems: dict[str, Any] = {}
    for system_id in ("B1", "B2"):
        row = baseline.get("systems", {}).get(system_id)
        if not row:
            continue
        correct = int(row.get("judgments", {}).get("CORRECT", 0))
        systems[system_id] = {
            "completed": int(row.get("completed", 0)),
            "failed": int(row.get("failed", 0)),
            "correct": correct,
            "incorrect": int(
                row.get("judgments", {}).get("INCORRECT", 0)
            ),
            "not_attempted": int(
                row.get("judgments", {}).get("NOT_ATTEMPTED", 0)
            ),
            "accuracy_all_55": correct / summary["sample_size"],
        }
    a1_accuracy = summary["systems"]["A1"]["accuracy_all_55"]
    return {
        "available": True,
        "same_sample": True,
        "systems": systems,
        "a1_minus_b2_percentage_points": (
            round(
                (a1_accuracy - systems["B2"]["accuracy_all_55"]) * 100,
                2,
            )
            if "B2" in systems
            else None
        ),
        "caveat": "同样本但不同运行窗口；差异只作 Pilot 诊断，不作因果结论",
    }


def _aggregate(
    details: dict[str, Any],
    *,
    baseline_path: Path,
) -> dict[str, Any]:
    system = _system_summary(details, "A1")
    items = [row["systems"].get("A1", {}) for row in details["results"]]
    completed = [
        item for item in items if item.get("run_status") == "completed"
    ]
    system["accuracy_all_55"] = (
        system["judgments"].get("CORRECT", 0) / len(items)
        if items
        else 0.0
    )
    system["answered"] = sum(
        item.get("answer", {}).get("status") == "answered"
        for item in completed
    )
    system["refused"] = sum(
        item.get("answer", {}).get("status") == "not_attempted"
        for item in completed
    )

    topic_rows: dict[str, Any] = {}
    for topic in EXPECTED_TOPIC_COUNTS:
        rows = [row for row in details["results"] if row["topic"] == topic]
        judgments = Counter(
            row["systems"].get("A1", {}).get("judgment", {}).get(
                "judgment", "UNJUDGED"
            )
            for row in rows
        )
        topic_rows[topic] = {"n": len(rows), "judgments": dict(judgments)}

    retry_history = [
        attempt
        for row in details["results"]
        for attempt in row.get("attempt_history", [])
    ]
    all_attempt_records = items + retry_history
    upstream_attempts = sum(
        int(item.get("upstream", {}).get("attempts", 0))
        for item in all_attempt_records
    )
    upstream_successes = sum(
        int(item.get("upstream", {}).get("successes", 0))
        for item in all_attempt_records
    )
    upstream_failure_codes: Counter[str] = Counter()
    logical_failure_codes: Counter[str] = Counter()
    for item in all_attempt_records:
        upstream_failure_codes.update(
            item.get("upstream", {}).get("failure_codes", {})
        )
        for event in item.get("run", {}).get("events", []):
            if event.get("tool") == "search" and not event.get("ok"):
                logical_failure_codes[
                    str(event.get("code") or "unknown")
                ] += 1

    all_events = [
        item.get("run", {}).get("events", []) for item in completed
    ]
    checks = {
        "selected_55": len(items) == 55,
        "five_per_topic": all(row["n"] == 5 for row in topic_rows.values()),
        "doubao_configured": details["provider"]["configured"],
        "backend_exactly_doubao": all(
            item.get("run", {}).get("search_backend") == "doubao"
            for item in completed
        ),
        "search_results_source_isolated": all(
            _search_events_are_single_source(events, "doubao")
            for events in all_events
        ),
        "leak_filter_self_test": details["self_tests"]["leak_filter"][
            "passed"
        ],
        "judge_self_test": details["self_tests"]["judge"]["passed"],
        "all_55_completed": len(completed) == 55,
        "all_55_judged": all(
            item.get("judgment", {}).get("judgment")
            in {"CORRECT", "INCORRECT", "NOT_ATTEMPTED"}
            for item in items
        ),
        "search_used": all(
            item.get("run", {}).get("tool_counts", {}).get("search", 0) >= 1
            for item in completed
        ),
        "usable_open": all(
            item.get("run", {}).get("tool_counts", {}).get(
                "open_url_usable", 0
            ) >= 1
            for item in completed
        ),
        "native_schema_without_repair": all(
            not item.get("run", {}).get("format_repaired", True)
            for item in completed
        ),
        "retrieved_answers_have_evidence": all(
            item.get("answer", {}).get("status") != "answered"
            or bool(item.get("answer", {}).get("evidence"))
            for item in completed
        ),
        "confidence_uses_percent_scale": all(
            not item.get("run", {}).get("confidence_scale_suspect", True)
            for item in completed
        ),
        "no_budget_violation": all(
            not item.get("run", {}).get("budget_violation", True)
            for item in completed
        ),
        "no_invalid_open_ref": all(
            "open_url ref 不是当前题" not in str(event.get("error") or "")
            for events in all_events
            for event in events
        ),
        "success_rate_at_least_98_percent": system["success_rate"] >= 0.98,
        "p95_within_180_seconds": (
            system["elapsed_p95_ms"] is not None
            and system["elapsed_p95_ms"] <= 180_000
        ),
    }
    self_test_calls = sum(
        row.get("model_calls", 0)
        for row in details["self_tests"]["judge"]["results"]
    )
    self_test_tokens = sum(
        row.get("usage", {}).get("total_tokens", 0)
        for row in details["self_tests"]["judge"]["results"]
    )
    summary = {
        "schema_version": "browsecomp-zh-doubao-pilot-summary.v1",
        "completed_at_utc": details.get("completed_at_utc"),
        "dataset_sha256": details["dataset_sha256"],
        "repo_commit": details["repo_commit"],
        "seed": details["seed"],
        "sample_size": len(items),
        "provider": details["provider"],
        "systems": {"A1": system},
        "topics": topic_rows,
        "search_diagnostics": {
            "upstream_attempts": upstream_attempts,
            "upstream_successes": upstream_successes,
            "upstream_failure_codes": dict(upstream_failure_codes),
            "logical_search_failure_codes": dict(logical_failure_codes),
        },
        "run_failure_types": dict(Counter(
            _failure_kind(item)
            for item in items
            if item.get("run_status") != "completed"
        )),
        "retry_history": {
            "prior_failed_attempts": len(retry_history),
            "prior_failure_types": dict(Counter(
                _failure_kind(item) for item in retry_history
            )),
        },
        "api_volume": {
            "model_calls": system["model_calls"] + self_test_calls,
            "model_tokens": system["tokens"] + self_test_tokens,
            "judge_self_test_calls": self_test_calls,
            "judge_self_test_tokens": self_test_tokens,
            "logical_search_calls": system["search_calls"],
            "doubao_upstream_attempts": upstream_attempts,
            "open_url_calls": system["open_calls"],
            "counts_are_lower_bounds": system["failed"] > 0,
            "lower_bound_scope": (
                "失败题的模型、逻辑搜索和读页调用；Doubao 上游尝试数已逐题保存"
            ),
        },
        "checks": checks,
        "passed": all(checks.values()),
        "reference_status": "official_answers_unaudited",
        "limitations": [
            "Pilot 只作运行诊断，不发布正式准确率。",
            "参考答案尚未完成双人盲化有效性审计。",
            "判分为内部 Judge，不是官方 GPT-4o 兼容通道。",
            "A1 与 B2 使用相同样本和协议，但不在同一运行窗口。",
        ],
    }
    summary["baseline_comparison"] = _baseline_comparison(
        summary,
        baseline_path,
    )
    return summary


def _render_report(summary: dict[str, Any]) -> str:
    row = summary["systems"]["A1"]
    judgments = row["judgments"]
    lines = [
        "# BrowseComp-ZH Doubao 单源 55 条 Pilot 运行诊断",
        "",
        f"- 完成时间：`{summary.get('completed_at_utc')}`",
        f"- 数据 SHA-256：`{summary['dataset_sha256']}`",
        f"- seed / 样本量：`{summary['seed']}` / `{summary['sample_size']}`",
        f"- 后端：`doubao`（direct provider adapter）",
        f"- 运行健康结论：**{'PASS' if summary['passed'] else 'FAIL'}**",
        "- 参考答案状态：`official_answers_unaudited`",
        "",
        "## A1 结果",
        "",
        "| 完成 | CORRECT | INCORRECT | 拒答 | 失败 | 全 55 条正确率 | "
        "search | open | usable | P50 ms | P95 ms |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| {row['completed']}/{row['runs']} | "
            f"{judgments.get('CORRECT', 0)} | "
            f"{judgments.get('INCORRECT', 0)} | "
            f"{judgments.get('NOT_ATTEMPTED', 0)} | {row['failed']} | "
            f"{row['accuracy_all_55']:.2%} | {row['search_calls']} | "
            f"{row['open_calls']} | {row['usable_opens']} | "
            f"{row['elapsed_p50_ms']} | {row['elapsed_p95_ms']} |"
        ),
        "",
        "## 与既有 Pilot 对照",
        "",
    ]
    baseline = summary["baseline_comparison"]
    if baseline.get("available"):
        lines += [
            "| 轨道 | 完成 | CORRECT | INCORRECT | 拒答 | 失败 | 全 55 条正确率 |",
            "|---|---:|---:|---:|---:|---:|---:|",
            (
                f"| A1 Doubao 单源多轮 | {row['completed']}/{row['runs']} | "
                f"{judgments.get('CORRECT', 0)} | "
                f"{judgments.get('INCORRECT', 0)} | "
                f"{judgments.get('NOT_ATTEMPTED', 0)} | {row['failed']} | "
                f"{row['accuracy_all_55']:.2%} |"
            ),
        ]
        for system_id, base in baseline["systems"].items():
            lines.append(
                f"| {system_id} 既有主 Pilot | "
                f"{base['completed']}/{summary['sample_size']} | "
                f"{base['correct']} | {base['incorrect']} | "
                f"{base['not_attempted']} | {base['failed']} | "
                f"{base['accuracy_all_55']:.2%} |"
            )
        lines += [
            "",
            "A1 主要应与同为多轮 Agent 的 B2 比较；"
            f"A1−B2 为 `{baseline['a1_minus_b2_percentage_points']:+.2f}` 个百分点。",
            f"{baseline['caveat']}。",
        ]
    else:
        lines.append(f"未载入可比基线：`{baseline.get('reason')}`。")

    diagnostics = summary["search_diagnostics"]
    volume = summary["api_volume"]
    retry_history = summary["retry_history"]
    lines += [
        "",
        "## 搜索与 API 调用",
        "",
        f"- 逻辑 search：`{volume['logical_search_calls']}`；"
        f"Doubao 上游尝试：`{volume['doubao_upstream_attempts']}`；"
        f"成功返回：`{diagnostics['upstream_successes']}`。",
        f"- 上游失败码（含重试后成功的失败尝试）："
        f"`{json.dumps(diagnostics['upstream_failure_codes'], ensure_ascii=False)}`。",
        f"- 模型调用{'至少' if volume['counts_are_lower_bounds'] else ''}："
        f"`{volume['model_calls']}`；Token：`{volume['model_tokens']}`。",
        f"- 网页读取{'至少' if volume['counts_are_lower_bounds'] else ''}："
        f"`{volume['open_url_calls']}`。",
        f"- 断点重跑前失败尝试：`{retry_history['prior_failed_attempts']}`；"
        f"类型：`{json.dumps(retry_history['prior_failure_types'], ensure_ascii=False)}`。",
        "",
        "## 验收检查",
        "",
        "| 检查 | 结果 |",
        "|---|---:|",
    ]
    for name, passed in summary["checks"].items():
        lines.append(f"| `{name}` | {'PASS' if passed else 'FAIL'} |")

    lines += [
        "",
        "## Topic 覆盖",
        "",
        "| Topic | n | CORRECT | INCORRECT | 拒答 | 未判 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for topic, topic_row in summary["topics"].items():
        counts = topic_row["judgments"]
        lines.append(
            f"| {topic} | {topic_row['n']} | "
            f"{counts.get('CORRECT', 0)} | {counts.get('INCORRECT', 0)} | "
            f"{counts.get('NOT_ATTEMPTED', 0)} | "
            f"{counts.get('UNJUDGED', 0)} |"
        )
    lines += [
        "",
        "## 限制",
        "",
        *[f"- {item}" for item in summary["limitations"]],
        "",
        "明文题目、答案、canary、模型答案、URL 与完整工具轨迹仅保存在"
        "受限运行目录，不进入本报告。",
        "",
    ]
    return "\n".join(lines)


def run_pilot(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    rows = load_encrypted_dataset(Path(args.dataset).resolve())
    selected = select_pilot(rows, seed=args.seed, per_topic=args.per_topic)
    if len(selected) != args.sample_size:
        raise ValueError(
            f"分层抽样得到 {len(selected)} 条，不等于 sample-size {args.sample_size}"
        )

    secure_dir = (
        Path(args.secure_run_dir).resolve()
        if args.secure_run_dir
        else Path(tempfile.mkdtemp(prefix="browsecomp-zh-doubao-pilot-run."))
    )
    secure_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure_dir.chmod(0o700)
    details_path = secure_dir / "doubao_pilot_details.json"

    settings = Settings.from_env()
    if "doubao" not in settings.enabled_providers:
        raise ValueError("A1 Pilot 缺少 DOUBAO_API_KEY")
    provider = DoubaoSearchProvider(
        api_key=settings.doubao_api_key,
        timeout=settings.provider_timeout,
        uvx_path=settings.doubao_uvx_path,
    )
    inner = SingleSourceSearchClient(
        provider,
        timeout=float(settings.provider_timeout),
    )
    search = RetryingSingleSourceSearchClient(
        inner,
        timeout=args.search_timeout,
        max_attempts=args.search_max_attempts,
        backoff_seconds=args.search_retry_backoff,
    )
    model = ModelClient(settings, model=args.model, timeout=args.model_timeout)
    judge_model = ModelClient(
        settings,
        model=args.judge_model,
        timeout=args.model_timeout,
    )
    finalizer = AnswerFinalizer(model)
    page_reader = PageReader(timeout=args.page_timeout)
    agent = B2Agent(
        model=model,
        finalizer=finalizer,
        search=search,
        page_reader=page_reader,
    )
    self_tests = {
        "leak_filter": _leak_filter_self_test(),
        "judge": _judge_self_test(judge_model),
    }
    if not all(test["passed"] for test in self_tests.values()):
        search.close()
        raise ValueError("Pilot 前自检失败")

    protocol = {
        "system": "A1",
        "search_backend": "doubao",
        "search_adapter": "direct_provider",
        "models": {
            "planner_answer": args.model,
            "judge": args.judge_model,
            "temperature": 0,
            "max_output_tokens": {
                "planner": 700,
                "finalizer": 700,
                "judge": 180,
            },
        },
        "budgets": {
            "search": 8,
            "open_url": 12,
            "evidence_chars": 80_000,
            "deadline_seconds": 180,
            "planner_turns": 14,
        },
        "timeouts": {
            "model_seconds": args.model_timeout,
            "search_total_seconds": args.search_timeout,
            "provider_attempt_seconds": settings.provider_timeout,
            "page_seconds": args.page_timeout,
        },
        "retry": {
            "model_max_attempts": 4,
            "search_max_attempts": args.search_max_attempts,
            "search_backoff_seconds": args.search_retry_backoff,
            "finalizer_output_or_invariant_max_attempts": 2,
            "judge_invalid_output_max_attempts": 2,
        },
        "judge": {
            "protocol": "internal_short_answer_equivalence",
            "reference_status": "official_answers_unaudited",
        },
    }
    freeze = {
        "finalizer_prompt_sha256": _freeze_hash(FINALIZER_SYSTEM),
        "planner_prompt_sha256": _freeze_hash(B2_PLANNER_SYSTEM),
        "judge_prompt_sha256": _freeze_hash(JUDGE_SYSTEM),
        "judge_schema_sha256": _freeze_hash(JUDGE_SCHEMA),
        "finalizer_schema_sha256": _freeze_hash(FINALIZER_SCHEMA),
        "tools_sha256": _freeze_hash([SEARCH_TOOL, OPEN_URL_TOOL]),
        "protocol_sha256": _freeze_hash(protocol),
    }

    if details_path.exists() and args.resume:
        details = json.loads(details_path.read_text(encoding="utf-8"))
        expected = {
            "dataset_sha256": EXPECTED_DATASET_SHA256,
            "seed": args.seed,
            "model": args.model,
            "judge_model": args.judge_model,
            "freeze": freeze,
            "protocol": protocol,
        }
        for key, value in expected.items():
            if details.get(key) != value:
                search.close()
                raise ValueError(f"resume 配置不匹配: {key}")
    else:
        if details_path.exists():
            search.close()
            raise ValueError(
                "运行目录已包含 doubao_pilot_details.json；"
                "使用 --resume 或换目录"
            )
        details = {
            "schema_version": "browsecomp-zh-doubao-pilot.v1",
            "started_at_utc": _utc_now(),
            "dataset_sha256": EXPECTED_DATASET_SHA256,
            "repo_commit": EXPECTED_REPO_COMMIT,
            "seed": args.seed,
            "per_topic": args.per_topic,
            "systems": ["A1"],
            "model": args.model,
            "judge_model": args.judge_model,
            "provider": {
                "id": "doubao",
                "configured": True,
                "snapshot": provider.descriptor.default_snapshot,
                "adapter": "direct_provider",
            },
            "freeze": freeze,
            "protocol": protocol,
            "self_tests": self_tests,
            "reference_status": "official_answers_unaudited",
            "results": [
                {
                    "sample_id": row["sample_id"],
                    "topic": row["Topic"],
                    "systems": {},
                }
                for row in selected
            ],
        }
        _secure_write(details_path, details)

    result_by_id = {row["sample_id"]: row for row in details["results"]}
    indexed_selected = list(enumerate(selected, 1))
    if args.resume:
        pending_rows = []
        failed_rows = []
        for index, row in indexed_selected:
            previous = result_by_id[row["sample_id"]]["systems"].get(
                "A1", {}
            )
            if previous.get("run_status") == "completed":
                continue
            target = (
                failed_rows
                if previous.get("run_status") == "failed"
                else pending_rows
            )
            target.append((index, row))
        execution_rows = pending_rows + failed_rows
        details.setdefault("resume_events", []).append({
            "resumed_at_utc": _utc_now(),
            "order": "pending_then_failed",
            "pending": len(pending_rows),
            "failed": len(failed_rows),
        })
        _secure_write(details_path, details)
    else:
        execution_rows = indexed_selected
    try:
        for index, row in execution_rows:
            item = result_by_id[row["sample_id"]]
            previous = item["systems"].get("A1", {})
            if args.resume and previous.get("run_status") == "failed":
                item.setdefault("attempt_history", []).append({
                    "run_status": "failed",
                    "error": previous.get("error", ""),
                    "elapsed_ms": previous.get("elapsed_ms", 0),
                    "upstream": previous.get("upstream", {}),
                    "archived_at_utc": _utc_now(),
                })
            print(
                f"[{index}/55] {row['sample_id'][:19]} "
                f"topic={row['Topic']}",
                flush=True,
            )
            before = search.snapshot()
            result = _run_system(
                system_id="A1",
                row=row,
                finalizer=finalizer,
                search=search,  # type: ignore[arg-type]
                b2_agent=agent,
                judge_model=judge_model,
            )
            result["upstream"] = RetryingSingleSourceSearchClient.delta(
                before,
                search.snapshot(),
            )
            item["systems"]["A1"] = result
            state = (
                result["judgment"]["judgment"]
                if result["run_status"] == "completed"
                else "ERROR " + result["error"][:160]
            )
            print(f"  A1: {state}", flush=True)
            _secure_write(details_path, details)
    finally:
        search.close()

    details["completed_at_utc"] = _utc_now()
    summary = _aggregate(
        details,
        baseline_path=Path(args.baseline_summary).resolve(),
    )
    details["checks"] = summary["checks"]
    details["passed"] = summary["passed"]
    _secure_write(details_path, details)
    return summary, secure_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--sample-size", type=int, default=55)
    parser.add_argument("--per-topic", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260730)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_MODEL)
    parser.add_argument("--model-timeout", type=float, default=90)
    parser.add_argument("--search-timeout", type=float, default=45)
    parser.add_argument("--search-max-attempts", type=int, default=2)
    parser.add_argument("--search-retry-backoff", type=float, default=0.5)
    parser.add_argument("--page-timeout", type=float, default=20)
    parser.add_argument("--secure-run-dir")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--baseline-summary",
        default="eval/browsecomp_zh_pilot_summary.json",
    )
    parser.add_argument("--summary-path", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT))
    args = parser.parse_args()
    if args.sample_size != args.per_topic * len(EXPECTED_TOPIC_COUNTS):
        raise SystemExit("sample-size 必须等于 per-topic × 11")

    summary, secure_dir = run_pilot(args)
    _atomic_json(Path(args.summary_path), summary)
    report = _render_report(summary)
    Path(args.report_path).write_text(report, encoding="utf-8")
    print()
    print(report)
    print(f"-> aggregate summary: {args.summary_path}")
    print(f"-> aggregate report: {args.report_path}")
    print(f"-> restricted details: {secure_dir}")
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
