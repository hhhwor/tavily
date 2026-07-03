"""组合式 domain reranker 契约测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import AcademicResult, PatentResult
from src.pipeline.rerank import AcademicReranker, PatentReranker, Reranker


class _FlatScorer(Reranker):
    name = "flat"

    def rerank(self, query, results, top_k):
        for r in results:
            r.rerank_score = 0.5
        return list(results)[:top_k]

    def score(self, query, texts):
        return [0.5 for _ in texts]


def test_academic_duplicate_or_empty_keys_do_not_drop_candidates():
    papers = [
        AcademicResult(url="", title="same", content="x", year=2025, citations=1),
        AcademicResult(url="", title="same", content="x", year=2025, citations=2),
        AcademicResult(url="", title="same", content="x", year=2025, citations=3),
    ]
    out = AcademicReranker(_FlatScorer()).rerank("same paper", papers, top_k=3)
    assert len(out) == 3
    assert {id(p) for p in out} == {id(p) for p in papers}


def test_patent_duplicate_or_empty_keys_do_not_drop_candidates():
    patents = [
        PatentResult(url="", title="same", content="x", application_number=""),
        PatentResult(url="", title="same", content="x", application_number=""),
        PatentResult(url="", title="same", content="x", application_number=""),
    ]
    out = PatentReranker(_FlatScorer()).rerank("same patent", patents, top_k=3)
    assert len(out) == 3
    assert {id(p) for p in out} == {id(p) for p in patents}


if __name__ == "__main__":
    test_academic_duplicate_or_empty_keys_do_not_drop_candidates()
    test_patent_duplicate_or_empty_keys_do_not_drop_candidates()
    print("OK: domain reranker contracts")
