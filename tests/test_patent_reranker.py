"""PatentReranker 测试。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import PatentResult
from src.pipeline.rerank import PatentReranker, Reranker, build_rerank_context


class _ConstantInner(Reranker):
    name = "const"

    def __init__(self, score: float = 0.7):
        self._score = score

    def rerank(self, query, results, top_k):
        for r in results:
            r.rerank_score = self._score
        return list(results)[:top_k]

    def score(self, query, texts):
        return [self._score for _ in texts]


class _KeywordInner(Reranker):
    name = "keyword"

    def rerank(self, query, results, top_k):
        for r in results:
            r.rerank_score = 1.0 if "foldable" in r.content.lower() else 0.1
        return sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)[:top_k]

    def score(self, query, texts):
        return [1.0 if "foldable" in t.lower() else 0.1 for t in texts]


class _NoOpInner(Reranker):
    name = "noop-like"

    def rerank(self, query, results, top_k):
        return list(results)[:top_k]

    def score(self, query, texts):
        return [0.0 for _ in texts]


def test_patent_reranker_uses_source_score_when_text_is_flat():
    patents = [
        PatentResult(
            url="u-low",
            title="battery device",
            content="battery control",
            score=3.0,
            publication_number="P1",
        ),
        PatentResult(
            url="u-high",
            title="battery device",
            content="battery control",
            score=10.0,
            publication_number="P2",
        ),
    ]
    rr = PatentReranker(_ConstantInner())
    out = rr.rerank("battery", patents, top_k=2)
    assert [p.publication_number for p in out] == ["P2", "P1"]


def test_patent_reranker_text_score_dominates_source_prior():
    patents = [
        PatentResult(
            url="u-source",
            title="display device",
            content="generic display",
            score=100.0,
            publication_number="P-source",
        ),
        PatentResult(
            url="u-text",
            title="foldable display device",
            content="foldable hinge display",
            score=1.0,
            publication_number="P-text",
        ),
    ]
    rr = PatentReranker(_KeywordInner())
    out = rr.rerank("foldable display", patents, top_k=2)
    assert out[0].publication_number == "P-text"


def test_patent_reranker_boosts_recent_patents_for_recent_queries():
    patents = [
        PatentResult(
            url="old",
            title="robot arm",
            content="robot arm",
            score=1.0,
            publication_number="old",
            publication_date="2018-01-01",
        ),
        PatentResult(
            url="new",
            title="robot arm",
            content="robot arm",
            score=1.0,
            publication_number="new",
            publication_date="2026-05-01",
        ),
    ]
    rr = PatentReranker(_NoOpInner())
    ctx = build_rerank_context("latest robot arm patent", time_sensitive=True)
    out = rr.rerank_with_context("latest robot arm patent", patents, top_k=2, ctx=ctx)
    assert [p.publication_number for p in out] == ["new", "old"]


@pytest.mark.parametrize("query", ["实时机器人专利", "前沿机器人专利"])
def test_soft_freshness_terms_reach_reranking_context(query):
    assert build_rerank_context(query, time_sensitive=True).wants_recent is True


if __name__ == "__main__":
    test_patent_reranker_uses_source_score_when_text_is_flat()
    test_patent_reranker_text_score_dominates_source_prior()
    test_patent_reranker_boosts_recent_patents_for_recent_queries()
    print("OK: PatentReranker")
