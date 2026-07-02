"""验证 SiliconFlowReranker 截断逻辑:passage 超过 25 时按文档边界截断,只调 1 次 API。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import patch
from src.pipeline.rerank import SiliconFlowReranker
from src.models import SearchResult


def _make_fake_post(calls):
    def fake_post(url, headers, json, timeout):
        n = len(json["documents"])
        calls["n"] += 1
        calls["sizes"].append(n)
        assert n <= 25, f"单次请求文档数 {n} 超过 25"

        class R:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return {"results": [{"index": i, "relevance_score": 1.0 - i * 0.01} for i in range(n)]}
        return R()
    return fake_post


def test_short_docs_truncate_to_25_one_call():
    docs = [SearchResult(url=f"u{i}", title=f"doc {i}", content="x" * 50) for i in range(30)]
    rr = SiliconFlowReranker(api_key="k", chunk_max_chars=400, chunk_overlap=50)
    calls = {"n": 0, "sizes": []}
    with patch("src.pipeline.rerank._requests.post", side_effect=_make_fake_post(calls)):
        out = rr.rerank("q", docs, top_k=10)
    assert calls["n"] == 1, calls
    assert calls["sizes"] == [25], calls
    assert len(out) == 10
    truncated = [d for d in docs if d.title in {f"doc {i}" for i in range(25, 30)}]
    assert all((d.rerank_score or 0) == 0 for d in truncated)


def test_multichunk_docs_single_call_within_limit():
    longdocs = [SearchResult(url=f"L{i}", title=f"L{i}", content="句子。" * 200) for i in range(5)]
    rr = SiliconFlowReranker(api_key="k", chunk_max_chars=400, chunk_overlap=50)
    calls = {"n": 0, "sizes": []}
    with patch("src.pipeline.rerank._requests.post", side_effect=_make_fake_post(calls)):
        rr.rerank("q", longdocs, top_k=5)
    assert calls["n"] == 1, calls
    assert all(s <= 25 for s in calls["sizes"]), calls


if __name__ == "__main__":
    test_short_docs_truncate_to_25_one_call()
    test_multichunk_docs_single_call_within_limit()
    print("OK: 截断逻辑通过(单次调用、<=25、文档边界、被截文档0分)")
