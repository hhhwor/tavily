"""Secure 55-item BrowseComp-ZH Pilot runner.

The encrypted benchmark is decrypted in memory. Plaintext questions, answers,
canaries, model answers, URLs, and passages are written only to a caller-owned
restricted run directory; repository artifacts contain aggregate diagnostics.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import statistics
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from eval.browsecomp_zh_eval import (
    B2_PLANNER_SYSTEM,
    FINALIZER_SCHEMA,
    FINALIZER_SYSTEM,
    JUDGE_SYSTEM,
    JUDGE_SCHEMA,
    OPEN_URL_TOOL,
    SEARCH_TOOL,
    AnswerFinalizer,
    B2Agent,
    BudgetController,
    EvidenceRegistry,
    ModelClient,
    PageReader,
    SearchClient,
    _atomic_json,
    _judge,
    _judge_self_test,
    _leak_filter_self_test,
    _normalize_search,
    _stable_id,
    _utc_now,
    _validate_health,
)
from src.config import Settings


EXPECTED_DATASET_SHA256 = (
    "49963cdc8b4a16f4656bbac89ed5f3495f7b3bec4cf310990f567e7893c6a531"
)
EXPECTED_REPO_COMMIT = "86abe635e7deef89ec00c68ff1c2588f0e2f2099"
EXPECTED_TOPIC_COUNTS = {
    "影视": 45,
    "艺术": 40,
    "地理": 37,
    "音乐": 32,
    "历史": 29,
    "医学": 26,
    "电子游戏": 23,
    "科技": 22,
    "体育": 18,
    "政策法规": 10,
    "学术论文": 7,
}
DEFAULT_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
DEFAULT_SUMMARY = Path("eval/browsecomp_zh_pilot_summary.json")
DEFAULT_REPORT = Path("eval/browsecomp_zh_pilot_report.md")

_XLSX_NAMESPACE = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _xlsx_table(path: Path) -> list[dict[str, str]]:
    """Read the benchmark's simple shared-string XLSX without extra packages."""
    with zipfile.ZipFile(path) as archive:
        shared_root = ElementTree.fromstring(
            archive.read("xl/sharedStrings.xml")
        )
        shared = [
            "".join(
                text.text or ""
                for text in item.findall(".//x:t", _XLSX_NAMESPACE)
            )
            for item in shared_root.findall("x:si", _XLSX_NAMESPACE)
        ]
        sheet_root = ElementTree.fromstring(
            archive.read("xl/worksheets/sheet1.xml")
        )

    matrix: list[dict[int, str]] = []
    for row in sheet_root.findall(".//x:sheetData/x:row", _XLSX_NAMESPACE):
        values: dict[int, str] = {}
        for cell in row.findall("x:c", _XLSX_NAMESPACE):
            reference = str(cell.get("r") or "")
            match = re.match(r"([A-Z]+)", reference)
            if not match:
                continue
            column = 0
            for character in match.group(1):
                column = column * 26 + ord(character) - ord("A") + 1
            value_node = cell.find("x:v", _XLSX_NAMESPACE)
            raw = value_node.text if value_node is not None else ""
            if cell.get("t") == "s" and raw:
                value = shared[int(raw)]
            else:
                value = raw or ""
            values[column - 1] = value
        matrix.append(values)

    if not matrix:
        raise ValueError("加密 XLSX 不包含数据")
    headers = {
        index: value.strip()
        for index, value in matrix[0].items()
        if value.strip()
    }
    required = {"Topic", "Question", "Answer", "canary"}
    if set(headers.values()) != required:
        raise ValueError(
            f"加密 XLSX 字段不符: {sorted(headers.values())}"
        )
    return [
        {
            name: row.get(index, "")
            for index, name in headers.items()
        }
        for row in matrix[1:]
    ]


def _decrypt(ciphertext: str, password: str) -> str:
    encrypted = base64.b64decode(ciphertext, validate=True)
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    key = (digest * (len(encrypted) // len(digest) + 1))[:len(encrypted)]
    return bytes(
        left ^ right for left, right in zip(encrypted, key)
    ).decode("utf-8")


def load_encrypted_dataset(path: Path) -> list[dict[str, str]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != EXPECTED_DATASET_SHA256:
        raise ValueError(
            f"数据 SHA-256 不符: expected={EXPECTED_DATASET_SHA256}, "
            f"actual={digest}"
        )
    encrypted_rows = _xlsx_table(path)
    rows: list[dict[str, str]] = []
    for encrypted in encrypted_rows:
        canary = encrypted["canary"]
        if not canary:
            raise ValueError("数据行缺少 canary")
        row = {
            field: _decrypt(encrypted[field], canary).strip()
            for field in ("Topic", "Question", "Answer")
        }
        if not all(row.values()):
            raise ValueError("解密后存在空字段")
        row["canary"] = canary
        row["sample_id"] = _stable_id(row["Question"])
        rows.append(row)

    if len(rows) != 289:
        raise ValueError(f"正式数据应为 289 条，实际为 {len(rows)}")
    counts = Counter(row["Topic"] for row in rows)
    if dict(counts) != EXPECTED_TOPIC_COUNTS:
        raise ValueError(
            f"Topic 分布不符: expected={EXPECTED_TOPIC_COUNTS}, "
            f"actual={dict(counts)}"
        )
    if len({row["sample_id"] for row in rows}) != len(rows):
        raise ValueError("数据包含重复 Question/sample_id")
    return rows


def select_pilot(
    rows: list[dict[str, str]],
    *,
    seed: int,
    per_topic: int = 5,
) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["Topic"]].append(row)

    topic_order = sorted(EXPECTED_TOPIC_COUNTS)
    random.Random(seed).shuffle(topic_order)
    selected: dict[str, list[dict[str, str]]] = {}
    for topic in topic_order:
        candidates = sorted(
            grouped[topic],
            key=lambda item: item["sample_id"],
        )
        topic_seed = int.from_bytes(
            hashlib.sha256(f"{seed}:{topic}".encode()).digest()[:8],
            "big",
        )
        selected[topic] = random.Random(topic_seed).sample(
            candidates,
            per_topic,
        )

    # Round-robin topics so no domain is concentrated in one time window.
    return [
        selected[topic][offset]
        for offset in range(per_topic)
        for topic in topic_order
    ]


def _secure_write(path: Path, value: Any) -> None:
    _atomic_json(path, value)
    path.chmod(0o600)


def _freeze_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999) - 1))
    return ordered[index]


def _system_summary(
    details: dict[str, Any],
    system_id: str,
) -> dict[str, Any]:
    items = [
        row["systems"].get(system_id, {})
        for row in details["results"]
    ]
    completed = [
        item for item in items if item.get("run_status") == "completed"
    ]
    judgments = Counter(
        item.get("judgment", {}).get("judgment", "UNJUDGED")
        for item in items
    )
    elapsed = [
        int(item.get("run", {}).get("elapsed_ms") or item.get("elapsed_ms") or 0)
        for item in items
        if item.get("run", {}).get("elapsed_ms") or item.get("elapsed_ms")
    ]
    answer_tokens = sum(
        item.get("run", {}).get("usage", {}).get("total_tokens", 0)
        for item in completed
    )
    judge_tokens = sum(
        item.get("judge_run", {}).get("usage", {}).get(
            "total_tokens", 0
        )
        for item in completed
    )
    model_calls = sum(
        int(
            item.get("run", {}).get(
                "model_calls",
                item.get("run", {}).get("planner_model_calls", 0)
                + item.get("run", {}).get("finalizer_model_calls", 0),
            )
        )
        + int(item.get("judge_run", {}).get("model_calls", 0))
        for item in completed
    )
    failures = Counter()
    for item in items:
        if item.get("run_status") != "failed":
            continue
        error = str(item.get("error") or "")
        if "HTTP 429" in error:
            kind = "model_http_429"
        elif "ReadTimeout" in error:
            kind = "model_read_timeout"
        elif "total call deadline" in error:
            kind = "model_total_deadline"
        elif "BudgetExceeded" in error:
            kind = "budget_exhausted"
        else:
            kind = error.split(":", 1)[0] or "unknown"
        failures[kind] += 1
    return {
        "runs": len(items),
        "completed": len(completed),
        "failed": len(items) - len(completed),
        "success_rate": (
            len(completed) / len(items) if items else 0.0
        ),
        "failure_types": dict(failures),
        "judgments": dict(judgments),
        "search_calls": sum(
            item.get("run", {}).get("tool_counts", {}).get("search", 0)
            for item in completed
        ),
        "open_calls": sum(
            item.get("run", {}).get("tool_counts", {}).get("open_url", 0)
            for item in completed
        ),
        "usable_opens": sum(
            item.get("run", {}).get("tool_counts", {}).get(
                "open_url_usable", 0
            )
            for item in completed
        ),
        "model_calls": model_calls,
        "answer_tokens": answer_tokens,
        "judge_tokens": judge_tokens,
        "tokens": answer_tokens + judge_tokens,
        "elapsed_p50_ms": (
            round(statistics.median(elapsed)) if elapsed else None
        ),
        "elapsed_p95_ms": _percentile(elapsed, 0.95),
        "budget_violations": sum(
            bool(item.get("run", {}).get("budget_violation", True))
            for item in completed
        ),
        "format_repairs": sum(
            bool(item.get("run", {}).get("format_repaired"))
            for item in completed
        ),
        "invalid_output_retries": sum(
            int(
                item.get("run", {}).get(
                    "invalid_output_attempts",
                    item.get("run", {}).get(
                        "finalizer_invalid_output_attempts", 0
                    ),
                )
            )
            + int(
                item.get("judge_run", {}).get(
                    "invalid_output_attempts", 0
                )
            )
            for item in completed
        ),
        "leak_hits": sum(
            int(item.get("run", {}).get("leak_hits", 0))
            for item in completed
        ),
    }


def _aggregate(details: dict[str, Any]) -> dict[str, Any]:
    systems = {
        system_id: _system_summary(details, system_id)
        for system_id in details["systems"]
    }
    topic_rows: dict[str, dict[str, Any]] = {}
    for topic in EXPECTED_TOPIC_COUNTS:
        rows = [
            row for row in details["results"] if row["topic"] == topic
        ]
        topic_rows[topic] = {
            "n": len(rows),
            "judgments": {
                system_id: dict(Counter(
                    row["systems"].get(system_id, {}).get(
                        "judgment", {}
                    ).get("judgment", "UNJUDGED")
                    for row in rows
                ))
                for system_id in details["systems"]
            },
        }

    all_items = [
        item
        for row in details["results"]
        for item in row["systems"].values()
    ]
    completed = [
        item for item in all_items if item.get("run_status") == "completed"
    ]
    checks = {
        "selected_55": len(details["results"]) == 55,
        "five_per_topic": all(
            row["n"] == 5 for row in topic_rows.values()
        ),
        "health_four_sources": details["health"]["providers"]
        == ["tencent", "baidu", "doubao", "aliyun"],
        "leak_filter_self_test": details["self_tests"][
            "leak_filter"
        ]["passed"],
        "judge_self_test": details["self_tests"]["judge"]["passed"],
        "all_165_completed": len(completed) == 165,
        "all_165_judged": all(
            item.get("judgment", {}).get("judgment")
            in {"CORRECT", "INCORRECT", "NOT_ATTEMPTED"}
            for item in all_items
        ),
        "b1_exactly_one_search": all(
            row["systems"].get("B1", {}).get("run", {}).get(
                "tool_counts", {}
            ).get("search") == 1
            for row in details["results"]
        ),
        "b2_search_used": all(
            row["systems"].get("B2", {}).get("run", {}).get(
                "tool_counts", {}
            ).get("search", 0) >= 1
            for row in details["results"]
        ),
        "b2_usable_open": all(
            row["systems"].get("B2", {}).get("run", {}).get(
                "tool_counts", {}
            ).get("open_url_usable", 0) >= 1
            for row in details["results"]
        ),
        "native_schema_without_repair": all(
            not item.get("run", {}).get("format_repaired", True)
            for item in completed
        ),
        "retrieved_answers_have_evidence": all(
            item.get("answer", {}).get("status") != "answered"
            or bool(item.get("answer", {}).get("evidence"))
            for row in details["results"]
            for item in (
                row["systems"].get("B1", {}),
                row["systems"].get("B2", {}),
            )
        ),
        "confidence_uses_percent_scale": all(
            not item.get("run", {}).get(
                "confidence_scale_suspect", True
            )
            for item in completed
        ),
        "no_budget_violation": all(
            not item.get("run", {}).get("budget_violation", True)
            for item in completed
        ),
        "no_invalid_open_ref": all(
            "open_url ref 不是当前题" not in str(
                event.get("error") or ""
            )
            for item in completed
            for event in item.get("run", {}).get("events", [])
        ),
    }
    self_test_tokens = sum(
        row.get("usage", {}).get("total_tokens", 0)
        for row in details["self_tests"]["judge"]["results"]
    )
    self_test_calls = sum(
        row.get("model_calls", 0)
        for row in details["self_tests"]["judge"]["results"]
    )
    logical_search_calls = sum(
        row["search_calls"] for row in systems.values()
    )
    previous_judge_calls = int(
        details.get("judge_revision", {}).get(
            "previous_judge_model_calls", 0
        )
    )
    previous_judge_tokens = int(
        details.get("judge_revision", {}).get(
            "previous_judge_model_tokens", 0
        )
    )
    return {
        "schema_version": "browsecomp-zh-pilot-summary.v1",
        "completed_at_utc": details.get("completed_at_utc"),
        "rejudged_at_utc": details.get("rejudged_at_utc"),
        "dataset_sha256": details["dataset_sha256"],
        "repo_commit": details["repo_commit"],
        "seed": details["seed"],
        "sample_size": len(details["results"]),
        "systems": systems,
        "topics": topic_rows,
        "api_volume": {
            "model_calls": (
                sum(row["model_calls"] for row in systems.values())
                + self_test_calls
                + previous_judge_calls
            ),
            "model_tokens": (
                sum(row["tokens"] for row in systems.values())
                + self_test_tokens
                + previous_judge_tokens
            ),
            "judge_self_test_calls": self_test_calls,
            "judge_self_test_tokens": self_test_tokens,
            "previous_judge_model_calls": previous_judge_calls,
            "previous_judge_model_tokens": previous_judge_tokens,
            "logical_search_calls": logical_search_calls,
            "known_search_provider_request_ceiling": (
                logical_search_calls * 4
            ),
            "open_url_calls": sum(
                row["open_calls"] for row in systems.values()
            ),
            "counts_are_lower_bounds": any(
                row["failed"] for row in systems.values()
            ),
            "lower_bound_reason": (
                "失败轨的部分模型、搜索和读页调用未落入完成态 run 快照"
            ),
        },
        "checks": checks,
        "passed": all(checks.values()),
        "reference_status": "official_answers_unaudited",
        "limitations": [
            "Pilot 只作运行诊断，不发布正式准确率。",
            "参考答案尚未完成双人盲化有效性审计。",
            "判分为内部 Judge，不是官方 GPT-4o 兼容通道。",
        ],
    }


def _render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# BrowseComp-ZH 55 条 Pilot 运行诊断",
        "",
        f"- 完成时间：`{summary.get('completed_at_utc')}`",
        f"- Judge 重判时间：`{summary.get('rejudged_at_utc')}`",
        f"- 数据 SHA-256：`{summary['dataset_sha256']}`",
        f"- 固定 commit：`{summary['repo_commit']}`",
        f"- seed / 样本量：`{summary['seed']}` / `{summary['sample_size']}`",
        f"- 运行健康结论：**{'PASS' if summary['passed'] else 'FAIL'}**",
        "- 参考答案状态：`official_answers_unaudited`",
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
        "## 分轨运行",
        "",
        "| 轨道 | 完成 | CORRECT | INCORRECT | 拒答 | 模型调用 | search | "
        "open | usable | Token | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system_id, row in summary["systems"].items():
        judgments = row["judgments"]
        lines.append(
            f"| {system_id} | {row['completed']}/{row['runs']} | "
            f"{judgments.get('CORRECT', 0)} | "
            f"{judgments.get('INCORRECT', 0)} | "
            f"{judgments.get('NOT_ATTEMPTED', 0)} | "
            f"{row['model_calls']} | {row['search_calls']} | "
            f"{row['open_calls']} | "
            f"{row['usable_opens']} | {row['tokens']} | "
            f"{row['elapsed_p50_ms']} | {row['elapsed_p95_ms']} |"
        )

    lines += [
        "",
        "## API 调用量",
        "",
        f"- 模型调用{'至少' if summary['api_volume']['counts_are_lower_bounds'] else ''}："
        f"`{summary['api_volume']['model_calls']}`；"
        f"Token：`{summary['api_volume']['model_tokens']}`（含 Judge）。",
        f"- Chukonu 逻辑搜索"
        f"{'至少' if summary['api_volume']['counts_are_lower_bounds'] else ''}："
        f"`{summary['api_volume']['logical_search_calls']}`；"
        "已记录搜索对应的四源上游请求理论上限："
        f"`{summary['api_volume']['known_search_provider_request_ceiling']}`。",
        f"- 网页读取"
        f"{'至少' if summary['api_volume']['counts_are_lower_bounds'] else ''}："
        f"`{summary['api_volume']['open_url_calls']}`。",
        "",
        "## Topic 覆盖",
        "",
        "| Topic | n | B0 正确 | B1 正确 | B2 正确 |",
        "|---|---:|---:|---:|---:|",
    ]
    for topic, row in summary["topics"].items():
        lines.append(
            f"| {topic} | {row['n']} | "
            f"{row['judgments']['B0'].get('CORRECT', 0)} | "
            f"{row['judgments']['B1'].get('CORRECT', 0)} | "
            f"{row['judgments']['B2'].get('CORRECT', 0)} |"
        )

    lines += [
        "",
        "## 限制",
        "",
        *[f"- {item}" for item in summary["limitations"]],
        "",
        "明文题目、答案、canary、模型答案与工具轨迹仅保存在受限运行目录，"
        "不进入本报告。",
        "",
    ]
    return "\n".join(lines)


def rejudge_pilot(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], Path]:
    """Re-score saved candidates after a Judge-only protocol correction."""
    secure_dir = Path(args.secure_run_dir).resolve()
    details_path = secure_dir / "pilot_details.json"
    if not details_path.exists():
        raise ValueError("rejudge-only 找不到 pilot_details.json")
    details = json.loads(details_path.read_text(encoding="utf-8"))
    if details.get("dataset_sha256") != EXPECTED_DATASET_SHA256:
        raise ValueError("rejudge-only 数据摘要不匹配")
    if details.get("seed") != args.seed:
        raise ValueError("rejudge-only seed 不匹配")

    rows = select_pilot(
        load_encrypted_dataset(Path(args.dataset).resolve()),
        seed=args.seed,
        per_topic=args.per_topic,
    )
    rows_by_id = {row["sample_id"]: row for row in rows}
    backup_path = secure_dir / "pilot_details.before_judge_v2.json"
    if not backup_path.exists():
        _secure_write(backup_path, details)
    previous_details = json.loads(
        backup_path.read_text(encoding="utf-8")
    )
    previous_items = [
        item
        for result in previous_details["results"]
        for item in result["systems"].values()
        if item.get("run_status") == "completed"
    ]
    previous_judge_calls = (
        sum(
            item.get("judge_run", {}).get("model_calls", 0)
            for item in previous_items
        )
        + sum(
            row.get("model_calls", 0)
            for row in previous_details["self_tests"]["judge"]["results"]
        )
    )
    previous_judge_tokens = (
        sum(
            item.get("judge_run", {}).get("usage", {}).get(
                "total_tokens", 0
            )
            for item in previous_items
        )
        + sum(
            row.get("usage", {}).get("total_tokens", 0)
            for row in previous_details["self_tests"]["judge"]["results"]
        )
    )

    pending_rejudge = any(
        item.get("run_status") == "completed"
        and item.get("judge_revision") != 2
        for result in details["results"]
        for item in result["systems"].values()
    )
    settings = Settings.from_env()
    judge_model = ModelClient(
        settings,
        model=args.judge_model,
        timeout=args.model_timeout,
    )
    if pending_rejudge:
        self_test = _judge_self_test(judge_model)
        if not self_test["passed"]:
            raise ValueError("修正后的 Judge 自检仍未通过")
    else:
        self_test = details["self_tests"]["judge"]
        if not self_test["passed"]:
            raise ValueError("保存的修正 Judge 自检未通过")

    old_schema_hash = details.get("freeze", {}).get(
        "judge_schema_sha256"
    )
    judge_revision = {
        "revision": 2,
        "reason": (
            "answered candidate 的模型 Judge 只允许 "
            "CORRECT/INCORRECT；NOT_ATTEMPTED 由评测器确定性处理"
        ),
        "old_schema_sha256": old_schema_hash,
        "new_schema_sha256": _freeze_hash(JUDGE_SCHEMA),
        "new_prompt_sha256": _freeze_hash(JUDGE_SYSTEM),
        "previous_judge_model_calls": previous_judge_calls,
        "previous_judge_model_tokens": previous_judge_tokens,
    }
    details["self_tests"]["judge"] = self_test
    details["judge_revision"] = judge_revision
    details["freeze"]["judge_schema_sha256"] = judge_revision[
        "new_schema_sha256"
    ]
    details["freeze"]["judge_prompt_sha256"] = judge_revision[
        "new_prompt_sha256"
    ]

    for index, result in enumerate(details["results"], 1):
        row = rows_by_id[result["sample_id"]]
        for system_id in details["systems"]:
            item = result["systems"].get(system_id, {})
            if item.get("run_status") != "completed":
                continue
            if item.get("judge_revision") == 2:
                continue
            judgment, judge_run = _judge(
                judge_model,
                question=row["Question"],
                answers=[row["Answer"]],
                candidate=item["answer"],
            )
            item["judgment"] = judgment
            item["judge_run"] = judge_run
            item["judge_revision"] = 2
            print(
                f"[rejudge {index}/55] {system_id}: "
                f"{judgment['judgment']}",
                flush=True,
            )
            _secure_write(details_path, details)

    details["rejudged_at_utc"] = _utc_now()
    aggregate = _aggregate(details)
    details["checks"] = aggregate["checks"]
    details["passed"] = aggregate["passed"]
    _secure_write(details_path, details)
    return aggregate, secure_dir


def _run_system(
    *,
    system_id: str,
    row: dict[str, str],
    finalizer: AnswerFinalizer,
    search: SearchClient,
    b2_agent: B2Agent,
    judge_model: ModelClient,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        if system_id == "B0":
            budget = BudgetController(
                max_searches=0,
                max_opens=0,
                max_evidence_chars=0,
                deadline_seconds=60,
            )
            answer, finalizer_run = finalizer.finalize(
                question=row["Question"],
                evidence_mode="no_search",
                registry=EvidenceRegistry(),
                budget=budget,
            )
            run = {
                **budget.snapshot(),
                "finalizer_model_ms": finalizer_run["model_ms"],
                "usage": finalizer_run["usage"],
                "model_calls": finalizer_run.get("model_calls", 0),
                "invalid_output_attempts": finalizer_run.get(
                    "invalid_output_attempts", 0
                ),
                "format_repaired": finalizer_run["format_repaired"],
                "evidence_refs": finalizer_run["evidence_refs"],
                "registry_size": finalizer_run["registry_size"],
                "confidence_scale_suspect": finalizer_run[
                    "confidence_scale_suspect"
                ],
                "leak_hits": 0,
            }
        elif system_id == "B1":
            budget = BudgetController(
                max_searches=1,
                max_opens=0,
                max_evidence_chars=12_000,
                deadline_seconds=60,
            )
            registry = EvidenceRegistry()
            budget.reserve_search()
            raw, search_ms = search.search(
                row["Question"],
                limit=8,
                timeout=budget.timeout(search.timeout),
            )
            hits, leak_hits = _normalize_search(
                raw,
                question=row["Question"],
                limit=8,
                canary=row["canary"],
            )
            for hit in hits:
                if budget.remaining_evidence_chars() <= 0:
                    break
                value = dict(hit)
                value["snippet"] = budget.consume_text(value["snippet"])
                if value["snippet"]:
                    registry.add_search_hit(value)
            answer, finalizer_run = finalizer.finalize(
                question=row["Question"],
                evidence_mode="retrieved",
                registry=registry,
                budget=budget,
            )
            run = {
                **budget.snapshot(),
                "finalizer_model_ms": finalizer_run["model_ms"],
                "usage": finalizer_run["usage"],
                "model_calls": finalizer_run.get("model_calls", 0),
                "invalid_output_attempts": finalizer_run.get(
                    "invalid_output_attempts", 0
                ),
                "format_repaired": finalizer_run["format_repaired"],
                "evidence_refs": finalizer_run["evidence_refs"],
                "registry_size": finalizer_run["registry_size"],
                "confidence_scale_suspect": finalizer_run[
                    "confidence_scale_suspect"
                ],
                "search_ms": search_ms,
                "search_status": raw.get("status"),
                "search_failures": raw.get("failures", []),
                "evidence_count": len(registry),
                "leak_hits": leak_hits,
            }
        else:
            answer, run = b2_agent.run(
                row["Question"],
                canary=row["canary"],
            )
        judgment, judge_run = _judge(
            judge_model,
            question=row["Question"],
            answers=[row["Answer"]],
            candidate=answer,
        )
        return {
            "run_status": "completed",
            "answer": answer,
            "judgment": judgment,
            "judge_run": judge_run,
            "run": run,
        }
    except Exception as exc:
        return {
            "run_status": "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
            "elapsed_ms": round(
                (time.perf_counter() - started) * 1000
            ),
        }


def run_pilot(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    dataset_path = Path(args.dataset).resolve()
    rows = load_encrypted_dataset(dataset_path)
    selected = select_pilot(
        rows,
        seed=args.seed,
        per_topic=args.per_topic,
    )
    if len(selected) != args.sample_size:
        raise ValueError(
            f"分层抽样得到 {len(selected)} 条，不等于 sample-size "
            f"{args.sample_size}"
        )

    secure_dir = (
        Path(args.secure_run_dir).resolve()
        if args.secure_run_dir
        else Path(tempfile.mkdtemp(prefix="browsecomp-zh-pilot-run."))
    )
    secure_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure_dir.chmod(0o700)
    details_path = secure_dir / "pilot_details.json"

    settings = Settings.from_env()
    health = _validate_health(args.health_url)
    model = ModelClient(
        settings,
        model=args.model,
        timeout=args.model_timeout,
    )
    judge_model = ModelClient(
        settings,
        model=args.judge_model,
        timeout=args.model_timeout,
    )
    search = SearchClient(
        settings,
        url=args.search_url,
        timeout=args.search_timeout,
    )
    page_reader = PageReader(timeout=args.page_timeout)
    finalizer = AnswerFinalizer(model)
    b2_agent = B2Agent(
        model=model,
        finalizer=finalizer,
        search=search,
        page_reader=page_reader,
    )
    self_tests = {
        "leak_filter": _leak_filter_self_test(),
        "judge": _judge_self_test(judge_model),
    }
    protocol = {
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
            "B0": {
                "search": 0,
                "open_url": 0,
                "evidence_chars": 0,
                "deadline_seconds": 60,
            },
            "B1": {
                "search": 1,
                "open_url": 0,
                "evidence_chars": 12_000,
                "deadline_seconds": 60,
            },
            "B2": {
                "search": 8,
                "open_url": 12,
                "evidence_chars": 80_000,
                "deadline_seconds": 180,
                "planner_turns": 14,
            },
        },
        "timeouts": {
            "model_seconds": args.model_timeout,
            "search_seconds": args.search_timeout,
            "page_seconds": args.page_timeout,
        },
        "retry": {
            "model_max_attempts": 4,
            "http_statuses": [429, 500, 502, 503, 504],
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
                raise ValueError(f"resume 配置不匹配: {key}")
    else:
        if details_path.exists():
            raise ValueError(
                f"运行目录已包含 pilot_details.json；使用 --resume 或换目录"
            )
        details = {
            "schema_version": "browsecomp-zh-pilot.v1",
            "started_at_utc": _utc_now(),
            "dataset_sha256": EXPECTED_DATASET_SHA256,
            "repo_commit": EXPECTED_REPO_COMMIT,
            "seed": args.seed,
            "per_topic": args.per_topic,
            "systems": ["B0", "B1", "B2"],
            "model": args.model,
            "judge_model": args.judge_model,
            "freeze": freeze,
            "protocol": protocol,
            "health": health,
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

    result_by_id = {
        item["sample_id"]: item for item in details["results"]
    }
    orders = [
        ("B0", "B1", "B2"),
        ("B1", "B2", "B0"),
        ("B2", "B0", "B1"),
    ]
    for index, row in enumerate(selected):
        item = result_by_id[row["sample_id"]]
        order = orders[index % len(orders)]
        print(
            f"[{index + 1}/55] {row['sample_id'][:19]} "
            f"topic={row['Topic']} order={'/'.join(order)}",
            flush=True,
        )
        for system_id in order:
            previous = item["systems"].get(system_id, {})
            if args.resume and previous.get("run_status") == "completed":
                print(f"  {system_id}: RESUME-SKIP", flush=True)
                continue
            result = _run_system(
                system_id=system_id,
                row=row,
                finalizer=finalizer,
                search=search,
                b2_agent=b2_agent,
                judge_model=judge_model,
            )
            item["systems"][system_id] = result
            if result["run_status"] == "completed":
                state = result["judgment"]["judgment"]
            else:
                state = "ERROR " + result["error"][:160]
            print(f"  {system_id}: {state}", flush=True)
            _secure_write(details_path, details)

    details["completed_at_utc"] = _utc_now()
    aggregate = _aggregate(details)
    details["checks"] = aggregate["checks"]
    details["passed"] = aggregate["passed"]
    _secure_write(details_path, details)
    return aggregate, secure_dir


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
    parser.add_argument("--page-timeout", type=float, default=20)
    parser.add_argument(
        "--search-url",
        default="http://127.0.0.1:8000/search",
    )
    parser.add_argument(
        "--health-url",
        default="http://127.0.0.1:8000/health",
    )
    parser.add_argument("--secure-run-dir")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rejudge-only", action="store_true")
    parser.add_argument("--summary-path", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT))
    args = parser.parse_args()
    if args.sample_size != args.per_topic * len(EXPECTED_TOPIC_COUNTS):
        raise SystemExit(
            "sample-size 必须等于 per-topic × 11"
        )

    if args.rejudge_only:
        if not args.secure_run_dir:
            raise SystemExit("--rejudge-only 需要 --secure-run-dir")
        summary, secure_dir = rejudge_pilot(args)
    else:
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
