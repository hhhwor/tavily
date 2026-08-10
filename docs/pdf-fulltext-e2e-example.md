# PDF 原文获取端到端测试案例

本文记录一个已经实际跑通的 Research PDF 原文获取案例，用于验证以下完整链路：

```text
Academic Search
  -> Research Seed
  -> deep_read
  -> OA PDF 下载与解析
  -> PDF 正文证据入库
  -> locator 解引用
  -> citation audit
```

## 1. 测试目标

测试不只检查搜索结果中是否存在 PDF URL，还需要确认：

1. Research Planner 生成了 `deep_read` 动作；
2. PDF 正文被解析为 `pdf_text` 类型的证据；
3. 正文证据携带稳定的文档版本和页码或分块定位信息；
4. locator 能从 SQLite 中持久化的原文精确解引用；
5. 最终引用审计通过，且不存在无效 locator。

## 2. 测试论文与研究问题

本案例使用以下开放获取论文：

- 标题：Spectrum-BERT: Pre-training of Deep Bidirectional Transformers for Spectral Classification of Chinese Liquors
- OpenAlex Work ID：`W4307310884`
- PDF：https://arxiv.org/pdf/2210.12440
- 搜索词：`Spectrum-BERT pre-training spectral classification Chinese liquors`
- 待验证主张：`Spectrum-BERT uses a pre-training and fine-tuning paradigm for spectral classification of Chinese liquors.`

## 3. 前置条件

- 项目 Python 环境位于 `.venv311`；
- 本地 OpenAlex 服务运行在 `http://localhost:9001`；
- `.env` 中启用了 Academic Search；
- PDF 正文模式为 `sync`，并允许至少读取 1 篇 PDF。

先检查 OpenAlex 服务：

```bash
curl -sS --max-time 5 http://localhost:9001/health
```

预期返回：

```json
{"status":"healthy"}
```

## 4. 执行端到端测试

在项目根目录执行：

```bash
.venv311/bin/python - <<'PY'
from dataclasses import replace
from pathlib import Path
import tempfile
import time

from src.application.commands import ResearchCommand
from src.bootstrap import build_container
from src.config import Settings
from src.domain.evidence import EvidenceLocator
from src.domain.research import CandidateClaimInput, ResearchObjective


query = "Spectrum-BERT pre-training spectral classification Chinese liquors"
claim = (
    "Spectrum-BERT uses a pre-training and fine-tuning paradigm for "
    "spectral classification of Chinese liquors."
)
terminal_states = {
    "completed",
    "partial",
    "failed",
    "cancelled",
    "needs_input",
}

with tempfile.TemporaryDirectory(prefix="tavily-pdf-e2e-") as temp_dir:
    settings = replace(
        Settings.from_env(),
        state_db_path=str(Path(temp_dir) / "state.sqlite3"),
        ranking_profile="fast",
        rerank_threshold_mode="off",
        rewrite_enabled=False,
        trust_verify_backend="rules",
        research_synthesis_enabled=False,
        research_max_workers=1,
        mcp_mode="false",
    )
    container = build_container(settings, include_mcp=False)
    try:
        search = container.engine.search(
            query,
            limit=5,
            source_types=("academic",),
        )
        assert search.research_seed is not None

        pdf_candidates = [
            item for item in search.evidence if item.access.oa_pdf_url
        ]
        assert pdf_candidates
        print(
            f"SEARCH status={search.status} "
            f"results={len(search.evidence)} "
            f"pdf_candidates={len(pdf_candidates)}"
        )

        task = container.engine.start_research(
            ResearchCommand(
                search_id=search.research_seed.search_id,
                profile="literature_review",
                depth="quick",
                objective=ResearchObjective(
                    question=claim,
                    claims=[CandidateClaimInput(text=claim)],
                ),
            ),
            idempotency_key="pdf-e2e-spectrum-bert-v1",
        )

        deadline = time.monotonic() + 45
        while (
            task.state not in terminal_states
            and time.monotonic() < deadline
        ):
            time.sleep(0.2)
            task = container.engine.get_research(
                task.research_id,
                detail="full",
            )

        assert task.dossier is not None
        actions = [
            action.kind
            for round_result in task.dossier.rounds
            for action in round_result.actions
        ]
        assert "deep_read" in actions

        pdf_evidence = [
            item
            for item in task.dossier.evidence_index.values()
            if item.passage.snippet_type == "pdf_text"
        ]
        assert pdf_evidence

        resolved_count = 0
        for item in pdf_evidence:
            locator = EvidenceLocator.model_validate(item.locator)
            resolved = container.research_store.resolve_locator(
                task.research_id,
                locator,
            )
            assert resolved == item.passage.text
            resolved_count += 1

        audit = task.dossier.citation_audit
        assert audit is not None
        assert audit.status == "passed"
        assert not audit.invalid_locator_refs

        sample = next(
            (
                item
                for item in pdf_evidence
                if item.locator is not None
                and item.locator.page_from is not None
            ),
            pdf_evidence[0],
        )
        locator = EvidenceLocator.model_validate(sample.locator)
        quote = " ".join(sample.passage.text.split())[:500]

        print(
            f"RESEARCH state={task.state} "
            f"actions={actions} "
            f"deep_read_docs={task.usage.deep_read_documents} "
            f"deep_read_pages={task.usage.deep_read_pages}"
        )
        print(
            f"PDF_EVIDENCE count={len(pdf_evidence)} "
            f"resolved={resolved_count} "
            f"version={locator.version_id} "
            f"page={locator.page_from}-{locator.page_to} "
            f"chunk={locator.chunk_index}"
        )
        print(f"QUOTE={quote}")
        print(
            f"CITATION_AUDIT status={audit.status} "
            f"coverage={audit.citation_coverage_rate:.2f} "
            f"invalid_locators={len(audit.invalid_locator_refs)}"
        )
        print("E2E_RESULT=PASS")
    finally:
        container.close()
PY
```

测试使用临时 SQLite 数据库，不会污染服务正在使用的 Research 状态库。关闭 Container 后，临时数据库会自动清理。

## 5. 实际测试结果

测试时间：2026-08-04。

关键结果如下：

```text
SEARCH status=complete results=5 pdf_candidates=2
RESEARCH state=partial actions=['deep_read'] deep_read_docs=1 deep_read_pages=8
PDF_EVIDENCE count=3 resolved=3
CITATION_AUDIT status=passed coverage=1.00 invalid_locators=0
E2E_RESULT=PASS
```

本次读取到的正文版本为：

```text
academic-version:57227953f762deeaf82351b54402d73550d9e86334b60c64213deeb002dd9e52
```

正文样例：

```text
IEEE TRANSACTIONS ON INSTRUMENTATION AND MEASUREMENT 1 Spectrum-BERT:
Pre-training of Deep Bidirectional Transformers for Spectral Classification
of Chinese Liquors ...
```

本次共采纳 3 段 `pdf_text` 证据，3 个 locator 都能从 SQLite 中保存的 PDF 原文精确反查。带页码的分块覆盖了第 5—10 页和第 10—12 页。

## 6. 结果解释

`E2E_RESULT=PASS` 是该案例的通过标志，表示 PDF 获取、解析、证据持久化、locator 解引用和引用审计全部成功。

Research 状态为 `partial` 不代表 PDF 获取失败。本案例使用 `quick` 深度，该深度最多执行 1 轮研究；当前只有一篇独立论文完成深读，因此任务以 `max_rounds_reached` 停止，结论覆盖度仍为不足。若要验证多来源结论覆盖，应改用 `standard` 或 `deep`，并为任务提供更多独立论文。

## 7. 常见失败定位

| 现象或错误码 | 含义 | 优先检查项 |
| --- | --- | --- |
| 没有 `research_seed` | 搜索结果未生成可继续研究的快照 | 搜索是否返回证据、状态库是否可写 |
| 没有 `deep_read` 动作 | Planner 没有发现需要正文补强的 Academic 证据 | 论文是否仍是 `abstract` / `discovery_only`，是否提供待验证主张 |
| `PDF_URL_MISSING` | 命中论文没有可用的 OA PDF 直链 | 更换开放获取论文，或检查 OpenAlex OA 字段 |
| `PDF_TEXT_TIMEOUT` / `DOWNLOAD_TIMEOUT` | PDF 下载或分页读取超时 | OpenAlex PDF 服务、外网连接和 PDF 超时预算 |
| `PDF_TEXT_READ_FAILED` | 已解析正文分页读取失败 | `/openalex/pdf/text/{work_id}` 服务和缓存状态 |
| 没有 `pdf_text` 证据 | PDF 未解析，或正文未被 Research 采纳 | Research failures、coverage gaps、PDF parser 日志 |
| locator 解引用结果不一致 | 正文版本或持久化索引不一致 | `version_id`、`chunk_index`、页码和 SQLite document read 记录 |
| `citation_audit.status=failed` | 最终陈述存在缺引、无效引用或不可解引用定位 | `invalid_locator_refs`、`unsupported_statement_refs` |
