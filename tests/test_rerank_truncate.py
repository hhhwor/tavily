"""验证 SiliconFlowReranker 默认不做 25 条本地截断。"""
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

        class R:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return {"results": [{"index": i, "relevance_score": 1.0 - i * 0.01} for i in range(n)]}
        return R()
    return fake_post


def test_short_docs_send_all_documents_in_one_call():
    docs = [SearchResult(url=f"u{i}", title=f"doc {i}", content="x" * 50) for i in range(30)]
    rr = SiliconFlowReranker(api_key="k", chunk_max_chars=400, chunk_overlap=50)
    calls = {"n": 0, "sizes": []}
    with patch("src.ranking.adapters.siliconflow.requests.post", side_effect=_make_fake_post(calls)):
        out = rr.rerank("q", docs, top_k=10)
    assert calls["n"] == 1, calls
    assert calls["sizes"] == [30], calls
    assert len(out) == 10
    assert all((d.rerank_score or 0) > 0 for d in out)
    assert all(d.rerank_score is None for d in docs)


def test_multichunk_docs_single_call_without_local_cap():
    longdocs = [SearchResult(url=f"L{i}", title=f"L{i}", content="句子。" * 500) for i in range(12)]
    rr = SiliconFlowReranker(api_key="k", chunk_max_chars=120, chunk_overlap=20)
    calls = {"n": 0, "sizes": []}
    with patch("src.ranking.adapters.siliconflow.requests.post", side_effect=_make_fake_post(calls)):
        rr.rerank("q", longdocs, top_k=5)
    assert calls["n"] == 1, calls
    assert calls["sizes"][0] > 25, calls


if __name__ == "__main__":
    test_short_docs_send_all_documents_in_one_call()
    test_multichunk_docs_single_call_without_local_cap()
    print("OK: SiliconFlowReranker 默认不做 25 条本地截断")
