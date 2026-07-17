"""F01 三种 Ranking Profile 与阈值模式的行为契约。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import AcademicResult, PatentResult, SearchResult
from src.pipeline.rerank import (
    AcademicReranker,
    NoOpReranker,
    PatentReranker,
    Reranker,
    WebReranker,
)


class _ByUrlScorer(Reranker):
    name = "by-url"

    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def rerank(self, query, results, top_k):
        values = self.score(query, [r.url for r in results])
        for result, score in zip(results, values):
            result.rerank_score = score
        return sorted(results, key=lambda r: r.rerank_score or 0, reverse=True)[:top_k]

    def score(self, query, texts):
        self.calls += 1
        return [
            next(
                (score for marker, score in self.scores.items() if marker in text),
                0.0,
            )
            for text in texts
        ]


class _MarkerScorer(Reranker):
    name = "marker"

    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def rerank(self, query, results, top_k):
        return list(results)[:top_k]

    def score(self, query, texts):
        self.calls += 1
        return [next(score for marker, score in self.scores.items() if marker in text) for text in texts]


class _BombScorer(Reranker):
    name = "must-not-run"

    def rerank(self, query, results, top_k):
        raise AssertionError("fast profile must not call text scorer")

    def score(self, query, texts):
        raise AssertionError("fast profile must not call text scorer")


def test_quality_uses_metadata_while_semantic_uses_only_text():
    papers = [
        AcademicResult(
            url="text-winner",
            title="same topic text-winner",
            content="same topic",
            score=1,
            citations=0,
            venue="arXiv",
            year=2024,
        ),
        AcademicResult(
            url="metadata-winner",
            title="same topic metadata-winner",
            content="same topic",
            score=1,
            citations=5000,
            venue="Nature",
            year=2024,
            is_oa=True,
            oa_pdf_url="https://example/paper.pdf",
        ),
    ]
    scores = {"text-winner": 0.9, "metadata-winner": 0.8}

    quality = AcademicReranker(
        _ByUrlScorer(scores), profile="quality", threshold_mode="off"
    ).rerank("same topic", [p.model_copy(deep=True) for p in papers], top_k=2)
    semantic = AcademicReranker(
        _ByUrlScorer(scores), profile="semantic", threshold_mode="off"
    ).rerank("same topic", [p.model_copy(deep=True) for p in papers], top_k=2)

    assert [p.url for p in quality] == ["metadata-winner", "text-winner"]
    assert [p.url for p in semantic] == ["text-winner", "metadata-winner"]


def test_fast_web_uses_rrf_without_calling_text_scorer():
    docs = [
        SearchResult(url="https://shared", title="shared", source="a", provider_rank=0),
        SearchResult(url="https://shared", title="shared", source="b", provider_rank=1),
        SearchResult(url="https://solo", title="solo", source="a", provider_rank=0),
    ]
    out = WebReranker(
        _BombScorer(), profile="fast", threshold_mode="strict"
    ).rerank("query", docs, top_k=2)

    assert out[0].url == "https://shared"
    assert len(out) == 2


def test_fast_vertical_uses_provider_score_without_calling_text_scorer():
    patents = [
        PatentResult(url="low", title="high text", content="high text", score=1),
        PatentResult(url="high", title="low text", content="low text", score=100),
    ]
    out = PatentReranker(
        _BombScorer(), profile="fast", threshold_mode="strict"
    ).rerank("high text", patents, top_k=2)

    assert [p.url for p in out] == ["high", "low"]


def _threshold_docs():
    return [
        SearchResult(
            url="https://below",
            title="below marker",
            content="below marker",
            source="a",
            provider_rank=0,
        ),
        SearchResult(
            url="https://pass",
            title="pass marker",
            content="pass marker",
            source="a",
            provider_rank=10,
        ),
    ]


def _threshold_rank(mode):
    return WebReranker(
        _MarkerScorer({"below marker": 0.29, "pass marker": 0.31}),
        profile="quality",
        threshold=0.3,
        threshold_mode=mode,
        pass_bonus=0.0,
    ).rerank("query", _threshold_docs(), top_k=2)


def test_threshold_off_uses_final_score_only():
    assert [r.url for r in _threshold_rank("off")] == ["https://below", "https://pass"]


def test_threshold_prefer_prioritizes_then_backfills():
    out = _threshold_rank("prefer")
    assert [r.url for r in out] == ["https://pass", "https://below"]
    assert len(out) == 2


def test_threshold_strict_filters_failed_candidates():
    assert [r.url for r in _threshold_rank("strict")] == ["https://pass"]


def test_threshold_is_inclusive():
    out = WebReranker(
        _MarkerScorer({"below marker": 0.3, "pass marker": 0.1}),
        profile="semantic",
        threshold=0.3,
        threshold_mode="strict",
    ).rerank("query", _threshold_docs(), top_k=2)
    assert [r.url for r in out] == ["https://below"]


def test_real_noop_scorer_never_filters_all_candidates():
    out = WebReranker(
        NoOpReranker(), profile="quality", threshold=0.9, threshold_mode="strict"
    ).rerank("query", _threshold_docs(), top_k=2)
    assert len(out) == 2
