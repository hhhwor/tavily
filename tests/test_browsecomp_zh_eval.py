from types import SimpleNamespace

import pytest

from eval.browsecomp_zh_eval import (
    AnswerFinalizer,
    BudgetController,
    EvidenceRegistry,
    JUDGE_SCHEMA,
    ModelReply,
    OPEN_URL_TOOL,
    SingleSourceSearchClient,
    _judge,
    _planner_tools,
    _search_events_are_single_source,
)
from eval.browsecomp_zh_doubao_pilot import (
    RetryingSingleSourceSearchClient,
)
from src.domain.errors import ExternalServiceError


class _FakeProvider:
    descriptor = SimpleNamespace(
        id="doubao",
        default_snapshot="fake-snapshot",
    )

    def __init__(self) -> None:
        self.request = None

    def retrieve(self, request):
        self.request = request
        document = SimpleNamespace(
            title="标题",
            url="https://example.com/result",
            snippet="摘要",
            content="正文",
            published_date="2024-01-01",
            source="doubao",
        )
        return SimpleNamespace(
            documents=(document,),
            snapshot="fake-snapshot",
            actual_query=request.query,
        )


def test_single_source_adapter_preserves_source_and_bounds_timeout():
    provider = _FakeProvider()
    client = SingleSourceSearchClient(provider, timeout=15)

    response, elapsed_ms = client.search("测试", limit=3, timeout=4)

    assert elapsed_ms >= 0
    assert provider.request.candidate_budget == 3
    assert provider.request.timeout_seconds == 4
    assert response["backend"] == "doubao"
    assert response["provider_snapshot"] == "fake-snapshot"
    assert response["evidence"] == [{
        "title": "标题",
        "url": "https://example.com/result",
        "passage": {"text": "正文"},
        "published_date": "2024-01-01",
        "source": "doubao",
    }]


def test_single_source_gate_rejects_mixed_or_missing_sources():
    isolated = [{
        "tool": "search",
        "ok": True,
        "backend": "doubao",
        "sources": ["doubao"],
    }]
    mixed = [{
        "tool": "search",
        "ok": True,
        "backend": "doubao",
        "sources": ["doubao", "baidu"],
    }]

    assert _search_events_are_single_source(isolated, "doubao")
    assert not _search_events_are_single_source(mixed, "doubao")
    assert not _search_events_are_single_source([], "doubao")


def test_single_source_retry_records_recoverable_attempt():
    class _FlakyClient:
        backend_id = "doubao"

        def __init__(self):
            self.calls = 0

        def search(self, query, *, limit, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise ExternalServiceError(
                    provider="doubao",
                    code="SEARCH_TIMEOUT",
                    recoverable=True,
                )
            return {"status": "complete"}, 1

        def close(self):
            pass

    inner = _FlakyClient()
    client = RetryingSingleSourceSearchClient(
        inner,  # type: ignore[arg-type]
        timeout=10,
        sleeper=lambda _: None,
    )

    response, elapsed_ms = client.search("测试", limit=3)

    assert response == {"status": "complete"}
    assert elapsed_ms >= 0
    assert inner.calls == 2
    assert client.snapshot() == {
        "attempts": 2,
        "successes": 1,
        "failure_codes": {"SEARCH_TIMEOUT": 1},
    }


def test_single_source_retry_stops_on_nonrecoverable_error():
    class _RejectedClient:
        backend_id = "doubao"

        def __init__(self):
            self.calls = 0

        def search(self, query, *, limit, timeout=None):
            self.calls += 1
            raise ExternalServiceError(
                provider="doubao",
                code="SEARCH_QUOTA_EXHAUSTED",
                recoverable=False,
            )

        def close(self):
            pass

    inner = _RejectedClient()
    client = RetryingSingleSourceSearchClient(
        inner,  # type: ignore[arg-type]
        timeout=10,
        sleeper=lambda _: None,
    )

    with pytest.raises(ExternalServiceError):
        client.search("测试", limit=3)

    assert inner.calls == 1


def test_retrieved_finalizer_refuses_without_registered_evidence():
    class _ModelMustNotRun:
        def call(self, *args, **kwargs):
            raise AssertionError("model must not run without evidence")

    answer, run = AnswerFinalizer(_ModelMustNotRun()).finalize(
        question="问题",
        evidence_mode="retrieved",
        registry=EvidenceRegistry(),
        budget=BudgetController(
            max_searches=1,
            max_opens=1,
            max_evidence_chars=1000,
            deadline_seconds=10,
        ),
    )

    assert answer["status"] == "not_attempted"
    assert answer["exact_answer"] == ""
    assert answer["evidence"] == []
    assert run["method"] == "deterministic_no_evidence"


def test_open_url_uses_registered_search_ref_instead_of_model_url():
    parameters = OPEN_URL_TOOL["function"]["parameters"]
    assert parameters["required"] == ["ref"]
    assert "ref" in parameters["properties"]
    assert "url" not in parameters["properties"]

    registry = EvidenceRegistry()
    search_entry = registry.add_search_hit({
        "title": "结果",
        "url": "https://example.com/result",
        "snippet": "摘要",
    })
    fallback_entry = registry.add_search_hit({
        "title": "备用结果",
        "url": "https://example.com/fallback",
        "snippet": "备用摘要",
    })
    page_entry = registry.add_page(
        url="https://example.com/result",
        text="页面正文",
    )[0]

    assert registry.resolve_search_ref(search_entry.ref) == search_entry
    tools = _planner_tools(
        registry,
        failed_open_refs={search_entry.ref},
    )
    ref_schema = tools[1]["function"]["parameters"]["properties"]["ref"]
    assert ref_schema["enum"] == [fallback_entry.ref]
    with pytest.raises(ValueError, match="不是当前题"):
        registry.resolve_search_ref(page_entry.ref)
    with pytest.raises(ValueError, match="不是当前题"):
        registry.resolve_search_ref("s999")


def test_judge_retries_invalid_output_with_strict_schema():
    class _JudgeModel:
        def __init__(self):
            self.calls = []

        def call(self, *args, **kwargs):
            self.calls.append(kwargs)
            content = (
                "not-json"
                if len(self.calls) == 1
                else '{"judgment":"INCORRECT","reason":""}'
            )
            return ModelReply(
                message={"role": "assistant", "content": content},
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                elapsed_ms=1,
            )

    model = _JudgeModel()
    judgment, run = _judge(
        model,
        question="问题",
        answers=["答案甲"],
        candidate={
            "status": "answered",
            "exact_answer": "答案乙",
            "confidence": 50,
            "explanation": "",
            "evidence": [],
        },
    )

    assert judgment["judgment"] == "INCORRECT"
    assert run["model_calls"] == 2
    assert run["invalid_output_attempts"] == 1
    assert run["usage"]["total_tokens"] == 30
    assert all(call["json_schema"] == JUDGE_SCHEMA for call in model.calls)
    assert JUDGE_SCHEMA["properties"]["judgment"]["enum"] == [
        "CORRECT",
        "INCORRECT",
    ]


def test_finalizer_retries_answered_without_evidence_ref():
    class _FinalizerModel:
        timeout = 10

        def __init__(self):
            self.calls = []

        def call(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            evidence_refs = [] if len(self.calls) == 1 else ["s1"]
            return ModelReply(
                message={
                    "role": "assistant",
                    "content": (
                        '{"status":"answered","exact_answer":"答案",'
                        '"confidence":80,"explanation":"",'
                        f'"evidence_refs":{evidence_refs!r}'
                        "}"
                    ).replace("'", '"'),
                },
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                elapsed_ms=1,
            )

    registry = EvidenceRegistry()
    registry.add_search_hit({
        "title": "结果",
        "url": "https://example.com/result",
        "snippet": "答案",
    })
    model = _FinalizerModel()
    answer, run = AnswerFinalizer(model).finalize(
        question="问题",
        evidence_mode="retrieved",
        registry=registry,
        budget=BudgetController(
            max_searches=1,
            max_opens=0,
            max_evidence_chars=1000,
            deadline_seconds=10,
        ),
    )

    assert answer["status"] == "answered"
    assert answer["evidence"][0]["url"] == "https://example.com/result"
    assert run["model_calls"] == 2
    assert run["invalid_output_attempts"] == 1
    assert "违反终态或证据引用不变量" in model.calls[1][0][-1]["content"]
