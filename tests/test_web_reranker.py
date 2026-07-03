"""WebReranker 测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import SearchResult
from src.pipeline.rerank import Reranker, WebReranker


class _ThresholdLikeInner(Reranker):
    name = "fake-threshold"

    def __init__(self):
        self.seen = 0

    def rerank(self, query, results, top_k):
        self.seen = len(results)
        for i, r in enumerate(results):
            r.rerank_score = 0.95 if i < 2 else 0.05
        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return [r for r in ranked if (r.rerank_score or 0.0) >= 0.3][:top_k]


class _FlatInner(Reranker):
    name = "flat"

    def rerank(self, query, results, top_k):
        for r in results:
            r.rerank_score = 0.8
        return list(results)[:top_k]


class _ReverseTextInner(Reranker):
    name = "reverse"

    def rerank(self, query, results, top_k):
        n = len(results)
        for i, r in enumerate(results):
            r.rerank_score = max(0.0, 1.0 - i / max(1, n))
        return sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)[:top_k]


def _make_results(order):
    rows = []
    for src in order:
        for i in range(6):
            rows.append(
                SearchResult(
                    url=f"https://{src}.example/{i}",
                    title=f"{src}-{i}",
                    snippet="diffusion survey",
                    content="diffusion survey " * 20,
                    source=src,
                    provider_rank=i,
                )
            )
    return rows


def test_web_reranker_scores_all_candidates_by_default():
    docs = [
        SearchResult(
            url=f"https://example.com/{i}",
            title=f"doc-{i}",
            snippet="survey on diffusion models",
            content="survey on diffusion models " * 50,
            source="baidu" if i % 2 else "tencent",
            provider_rank=i,
        )
        for i in range(30)
    ]
    inner = _ThresholdLikeInner()
    rr = WebReranker(inner, max_chars=180)
    out = rr.rerank("survey on diffusion models", docs, top_k=10)
    assert inner.seen == 30
    assert len(out) == 10
    assert [r.url for r in out[:2]] == ["https://example.com/0", "https://example.com/1"]


def test_web_reranker_uses_rrf_prior_when_text_scores_flat():
    docs = [
        SearchResult(
            url="https://shared.example/paper",
            title="shared",
            snippet="diffusion survey",
            content="diffusion survey",
            source="baidu",
            provider_rank=0,
        ),
        SearchResult(
            url="https://shared.example/paper",
            title="shared",
            snippet="diffusion survey",
            content="diffusion survey extended",
            source="tencent",
            provider_rank=1,
        ),
        SearchResult(
            url="https://solo.example/0",
            title="solo-0",
            snippet="diffusion survey",
            content="diffusion survey",
            source="baidu",
            provider_rank=0,
        ),
        SearchResult(
            url="https://solo.example/1",
            title="solo-1",
            snippet="diffusion survey",
            content="diffusion survey",
            source="tencent",
            provider_rank=0,
        ),
    ]
    rr = WebReranker(_FlatInner(), max_chars=180)
    out = rr.rerank("survey on diffusion models", docs, top_k=10)
    assert out[0].url == "https://shared.example/paper"
    assert "baidu" in out[0].source and "tencent" in out[0].source


def test_web_reranker_text_score_dominates_rrf_prior():
    docs = _make_results(["baidu", "tencent"])
    rr = WebReranker(_ReverseTextInner(), max_chars=180)
    out = rr.rerank("survey on diffusion models", docs, top_k=5)
    assert out[0].url == "https://baidu.example/0"
    assert out[1].url == "https://tencent.example/0"


def test_web_reranker_is_stable_across_provider_completion_order():
    rr = WebReranker(_FlatInner(), max_chars=180)
    out1 = rr.rerank("survey on diffusion models", _make_results(["baidu", "tencent"]), top_k=10)
    out2 = rr.rerank("survey on diffusion models", _make_results(["tencent", "baidu"]), top_k=10)
    assert [r.url for r in out1] == [r.url for r in out2]


if __name__ == "__main__":
    test_web_reranker_scores_all_candidates_by_default()
    test_web_reranker_uses_rrf_prior_when_text_scores_flat()
    test_web_reranker_text_score_dominates_rrf_prior()
    test_web_reranker_is_stable_across_provider_completion_order()
    print("OK: WebReranker")
