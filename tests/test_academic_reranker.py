"""AcademicReranker 测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import AcademicResult
from src.pipeline.rerank import AcademicReranker, Reranker


class _ConstantInner(Reranker):
    name = "const"

    def __init__(self, score: float = 0.8):
        self._score = score

    def rerank(self, query, results, top_k):
        for r in results:
            r.rerank_score = self._score
        return list(results)[:top_k]


class _NoScoreInner(Reranker):
    name = "noscore"

    def rerank(self, query, results, top_k):
        return list(results)[:top_k]


class _ThresholdLikeInner(Reranker):
    name = "fake-threshold"

    def __init__(self):
        self.seen = 0

    def rerank(self, query, results, top_k):
        self.seen = len(results)
        for i, r in enumerate(results):
            r.rerank_score = 0.95 if i < 5 else 0.05
        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return [r for r in ranked if (r.rerank_score or 0.0) >= 0.3][:top_k]


def test_academic_reranker_uses_citations_and_oa():
    papers = [
        AcademicResult(
            url="https://a",
            title="Diffusion Models",
            content="A survey of diffusion models.",
            venue="arXiv",
            citations=20,
            year=2026,
            is_oa=True,
            oa_pdf_url="https://a.pdf",
        ),
        AcademicResult(
            url="https://b",
            title="Diffusion Models",
            content="A survey of diffusion models.",
            venue="Nature Machine Intelligence",
            citations=300,
            year=2024,
            is_oa=False,
        ),
    ]
    rr = AcademicReranker(_ConstantInner())
    out = rr.rerank("diffusion models survey", papers, top_k=2)
    assert [p.url for p in out] == ["https://b", "https://a"]
    assert out[0].rerank_score is not None
    assert out[0].rerank_score > out[1].rerank_score


def test_academic_reranker_boosts_recent_papers_for_latest_queries():
    papers = [
        AcademicResult(
            url="https://old",
            title="Reasoning Model",
            content="Reasoning model overview.",
            venue="ICLR",
            citations=400,
            year=2021,
            date="2021-05-01",
        ),
        AcademicResult(
            url="https://new",
            title="Reasoning Model",
            content="Reasoning model overview.",
            venue="ICLR",
            citations=120,
            year=2026,
            date="2026-03-01",
        ),
    ]
    rr = AcademicReranker(_ConstantInner())
    out = rr.rerank("latest reasoning model", papers, top_k=2)
    assert [p.url for p in out] == ["https://new", "https://old"]


def test_academic_reranker_prefers_highly_cited_work_for_standard_queries():
    papers = [
        AcademicResult(
            url="https://classic",
            title="Graph Neural Networks",
            content="Graph neural networks overview.",
            venue="ICLR",
            citations=1200,
            year=2017,
            date="2017-05-01",
        ),
        AcademicResult(
            url="https://fresh",
            title="Graph Neural Networks",
            content="Graph neural networks overview.",
            venue="ICLR",
            citations=30,
            year=2026,
            date="2026-05-01",
        ),
    ]
    rr = AcademicReranker(_ConstantInner())
    out = rr.rerank("graph neural networks", papers, top_k=2)
    assert [p.url for p in out] == ["https://classic", "https://fresh"]


def test_academic_reranker_uses_recency_intent_to_counter_citation_prior():
    papers = [
        AcademicResult(
            url="https://classic",
            title="Reasoning Model",
            content="Reasoning model overview.",
            venue="ICLR",
            citations=450,
            year=2021,
            date="2021-05-01",
        ),
        AcademicResult(
            url="https://fresh",
            title="Reasoning Model",
            content="Reasoning model overview.",
            venue="ICLR",
            citations=120,
            year=2026,
            date="2026-03-01",
        ),
    ]
    rr = AcademicReranker(_ConstantInner())
    out = rr.rerank("latest reasoning model", papers, top_k=2)
    assert [p.url for p in out] == ["https://fresh", "https://classic"]


def test_academic_reranker_handles_noop_like_inner_without_scores():
    papers = [
        AcademicResult(
            url="https://x",
            title="Graph Retrieval",
            content="Graph retrieval methods.",
            score=10.0,
            venue="arXiv",
            citations=10,
        ),
        AcademicResult(
            url="https://y",
            title="Graph Retrieval",
            content="Graph retrieval methods.",
            score=10.0,
            venue="ACL",
            citations=120,
        ),
    ]
    rr = AcademicReranker(_NoScoreInner())
    out = rr.rerank("graph retrieval survey", papers, top_k=2)
    assert [p.url for p in out] == ["https://y", "https://x"]


def test_academic_reranker_uses_document_budget_and_backfills():
    papers = [
        AcademicResult(
            url=f"https://p{i}",
            title=f"Diffusion Survey {i}",
            content="diffusion models survey " * 120,
            score=100.0 - i,
            citations=i,
            year=2026 - (i % 3),
            venue="ACL" if i % 2 else "arXiv",
        )
        for i in range(10)
    ]
    inner = _ThresholdLikeInner()
    rr = AcademicReranker(inner, max_docs=25, max_chars=180)
    out = rr.rerank("diffusion models survey", papers, top_k=10)
    assert inner.seen == 10
    assert len(out) == 10
    assert all(p.rerank_score is not None for p in out)


if __name__ == "__main__":
    test_academic_reranker_uses_citations_and_oa()
    test_academic_reranker_boosts_recent_papers_for_latest_queries()
    test_academic_reranker_prefers_highly_cited_work_for_standard_queries()
    test_academic_reranker_uses_recency_intent_to_counter_citation_prior()
    test_academic_reranker_handles_noop_like_inner_without_scores()
    test_academic_reranker_uses_document_budget_and_backfills()
    print("OK: AcademicReranker")
