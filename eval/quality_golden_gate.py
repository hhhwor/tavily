"""Deterministic offline ranking quality gate.

Usage:
  python -m eval.quality_golden_gate
  python -m eval.quality_golden_gate --update-baseline
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

from eval import metrics as M
from src.domain.search import AcademicResult, PatentResult, SearchResult
from src.ranking import AcademicReranker, PatentReranker, WebReranker
from src.ranking.ports import Reranker


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "eval" / "golden" / "quality_corpus.json"
DEFAULT_BASELINE = ROOT / "eval" / "golden" / "quality_baseline.json"
DEFAULT_REPORT = ROOT / "eval" / "golden" / "quality_report.json"
RUNNER_VERSION = "quality-golden.v1"
METRIC_NAMES = ("ndcg_at_k", "recall_at_k", "precision_at_k", "mrr")
_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]", re.I)


class LexicalScorer(Reranker):
    """Stable scorer used only by the offline golden corpus."""

    name = "golden-token-overlap.v1"

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return set(_TOKEN.findall((value or "").lower()))

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        query_tokens = self._tokens(query)
        if not query_tokens:
            return [0.0 for _ in texts]
        scores = []
        for text in texts:
            document_tokens = self._tokens(text)
            overlap = len(query_tokens & document_tokens)
            recall = overlap / len(query_tokens)
            precision = overlap / max(1, len(document_tokens))
            scores.append(min(1.0, 0.85 * recall + 0.15 * precision))
        return scores

    def rerank(
        self,
        query: str,
        results: list[SearchResult],
        top_k: int,
    ) -> list[SearchResult]:
        scored = [item.model_copy(deep=True) for item in results]
        for item, score in zip(
            scored,
            self.score(query, [item.text_for_rerank() for item in scored]),
        ):
            item.rerank_score = score
        return sorted(
            scored,
            key=lambda item: item.rerank_score or 0.0,
            reverse=True,
        )[:top_k]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_corpus(path: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "quality-corpus.v1":
        raise ValueError("unsupported quality corpus version")
    if not data.get("queries"):
        raise ValueError("quality corpus must contain queries")
    return data


def _candidate(track: str, row: dict[str, Any]) -> SearchResult:
    payload = {
        key: value
        for key, value in row.items()
        if key not in {"id", "relevance"}
    }
    payload["raw"] = {"_golden_id": row["id"]}
    if track == "academic":
        return AcademicResult(**payload)
    if track == "patent":
        return PatentResult(**payload)
    return SearchResult(**payload)


def _reranker(track: str, scorer: Reranker) -> Reranker:
    options = {
        "profile": "quality",
        "threshold_mode": "off",
    }
    if track == "academic":
        return AcademicReranker(scorer, **options)
    if track == "patent":
        return PatentReranker(scorer, **options)
    return WebReranker(scorer, **options)


def evaluate_corpus(
    corpus: dict[str, Any],
) -> dict[str, Any]:
    k = int(corpus["k"])
    scorer = LexicalScorer()
    per_track: dict[str, list[dict[str, float]]] = {}
    query_rows = []
    for query in corpus["queries"]:
        track = query["track"]
        candidates = [
            _candidate(track, item) for item in query["candidates"]
        ]
        relevance = {
            item["id"]: int(item["relevance"])
            for item in query["candidates"]
        }
        ranked = _reranker(track, scorer).rerank(
            query["query"],
            candidates,
            k,
        )
        ranked_ids = [
            str(item.raw["_golden_id"])
            for item in ranked
        ]
        ranked_rels = [relevance[item_id] for item_id in ranked_ids]
        pool_rels = list(relevance.values())
        metrics = {
            "ndcg_at_k": M.ndcg_at_k(ranked_rels, pool_rels, k),
            "recall_at_k": M.recall_at_k(ranked_rels, pool_rels, k),
            "precision_at_k": M.precision_at_k(ranked_rels, k),
            "mrr": M.mrr(ranked_rels),
        }
        per_track.setdefault(track, []).append(metrics)
        query_rows.append({
            "id": query["id"],
            "track": track,
            "ranked_ids": ranked_ids,
            "metrics": metrics,
        })
    tracks = {
        track: M.aggregate(rows)
        for track, rows in sorted(per_track.items())
    }
    overall = M.aggregate([
        row["metrics"] for row in query_rows
    ])
    return {
        "runner_version": RUNNER_VERSION,
        "scorer": scorer.name,
        "query_count": len(query_rows),
        "k": k,
        "metrics": {"overall": overall, **tracks},
        "queries": query_rows,
    }


def compare_to_baseline(
    actual: dict[str, Any],
    baseline: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if actual["runner_version"] != baseline.get("runner_version"):
        failures.append("runner_version mismatch")
    expected_metrics = baseline.get("metrics", {})
    max_drop = baseline.get("max_drop", {})
    for track, expected in expected_metrics.items():
        observed = actual.get("metrics", {}).get(track)
        if observed is None:
            failures.append(f"missing track: {track}")
            continue
        for metric in METRIC_NAMES:
            floor = float(expected[metric]) - float(max_drop.get(metric, 0.0))
            if float(observed[metric]) + 1e-12 < floor:
                failures.append(
                    f"{track}.{metric}={observed[metric]:.6f} "
                    f"below floor={floor:.6f}"
                )
    return failures


def run_gate(
    *,
    corpus_path: Path = DEFAULT_CORPUS,
    baseline_path: Path = DEFAULT_BASELINE,
    report_path: Path | None = DEFAULT_REPORT,
    update_baseline: bool = False,
) -> dict[str, Any]:
    corpus = load_corpus(corpus_path)
    result = evaluate_corpus(corpus)
    result["corpus_sha256"] = _sha256(corpus_path)

    if update_baseline:
        baseline = {
            "schema_version": "quality-baseline.v1",
            "runner_version": RUNNER_VERSION,
            "corpus_sha256": result["corpus_sha256"],
            "query_count": result["query_count"],
            "k": result["k"],
            "max_drop": {
                "ndcg_at_k": 0.02,
                "recall_at_k": 0.02,
                "precision_at_k": 0.02,
                "mrr": 0.02,
            },
            "metrics": result["metrics"],
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("corpus_sha256") != result["corpus_sha256"]:
            raise RuntimeError(
                "quality corpus hash changed; review it and run "
                "--update-baseline explicitly"
            )

    failures = compare_to_baseline(result, baseline)
    result["status"] = "failed" if failures else "passed"
    result["failures"] = failures
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if failures:
        raise RuntimeError("quality golden gate failed: " + "; ".join(failures))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()
    result = run_gate(
        corpus_path=args.corpus,
        baseline_path=args.baseline,
        report_path=args.report,
        update_baseline=args.update_baseline,
    )
    print(json.dumps({
        "status": result["status"],
        "query_count": result["query_count"],
        "metrics": result["metrics"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
