"""Train and calibrate the Qwen3 embedding intent-routing linear heads.

Usage::

    .venv311/bin/python -m eval.train_embedding_intent_classifier

The script fetches frozen Qwen3 embeddings, trains one logistic head per source
type, jointly calibrates the three decision thresholds on the validation split,
and writes a production artifact.  Embeddings can be cached outside the repo
with ``--embedding-cache`` to make repeated calibration runs inexpensive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import requests

from eval.intent_route_corpus import (
    SOURCE_ORDER,
    IntentRouteCase,
    group_counts,
    training_cases,
    validation_cases,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT / "src/infrastructure/data/qwen3_embedding_intent_linear_v1.json"
)
DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
QUERY_PREFIX = (
    "Instruct: Classify whether the search query requires academic literature, "
    "patent documents, or Chinese legal sources. Multiple labels are allowed.\n"
    "Query: "
)


def _load_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            values[key.strip()] = value
    values.update(os.environ)
    return values


def _normalize_rows(values: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if np.any(norms <= 0.0):
        raise ValueError("embedding vector magnitude must be positive")
    return values / norms


def _parse_embeddings(payload: object, expected: int) -> np.ndarray:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("embedding response must contain a data array")
    indexed: dict[int, list[float]] = {}
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise ValueError("embedding data item must be an object")
        index = item.get("index")
        embedding = item.get("embedding")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < expected
            or index in indexed
            or not isinstance(embedding, list)
            or not embedding
        ):
            raise ValueError("embedding response contains invalid data")
        vector = [float(value) for value in embedding]
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("embedding vector must contain finite numbers")
        indexed[index] = vector
    if len(indexed) != expected:
        raise ValueError("embedding response count does not match input count")
    dimensions = {len(vector) for vector in indexed.values()}
    if len(dimensions) != 1:
        raise ValueError("embedding vectors must have one dimension")
    return _normalize_rows(np.asarray(
        [indexed[index] for index in range(expected)], dtype=np.float64
    ))


def _read_cache(path: Path | None) -> dict[str, list[float]]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("embedding cache must be a JSON object")
    return {
        str(key): [float(value) for value in vector]
        for key, vector in payload.items()
        if isinstance(vector, list)
    }


def _write_cache(path: Path | None, cache: dict[str, list[float]]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def fetch_embeddings(
    cases: Sequence[IntentRouteCase],
    *,
    api_key: str,
    base_url: str,
    model: str,
    batch_size: int,
    timeout: float,
    cache_path: Path | None,
) -> np.ndarray:
    cache = _read_cache(cache_path)
    cache_keys = [f"{model}\0{QUERY_PREFIX}{case.query}" for case in cases]
    missing = [
        (index, key, QUERY_PREFIX + case.query)
        for index, (case, key) in enumerate(zip(cases, cache_keys))
        if key not in cache
    ]
    session = requests.Session()
    for offset in range(0, len(missing), batch_size):
        batch = missing[offset:offset + batch_size]
        started = time.perf_counter()
        response = session.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": [text for _, _, text in batch],
                "encoding_format": "float",
            },
            timeout=(10, timeout),
        )
        response.raise_for_status()
        vectors = _parse_embeddings(response.json(), len(batch))
        for (_, key, _), vector in zip(batch, vectors):
            cache[key] = vector.tolist()
        _write_cache(cache_path, cache)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(
            f"embedded {min(offset + len(batch), len(missing))}/{len(missing)} "
            f"uncached cases in {elapsed_ms:.1f} ms"
        )
    matrix = np.asarray([cache[key] for key in cache_keys], dtype=np.float64)
    return _normalize_rows(matrix)


def _targets(cases: Sequence[IntentRouteCase]) -> np.ndarray:
    return np.asarray([
        [1.0 if source in case.source_types else 0.0 for source in SOURCE_ORDER]
        for case in cases
    ], dtype=np.float64)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def train_logistic_heads(
    features: np.ndarray,
    targets: np.ndarray,
    *,
    epochs: int,
    learning_rate: float,
    l2: float,
) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """Fit three independent class-balanced logistic heads with Adam."""
    rows, dimensions = features.shape
    weights = np.zeros((dimensions, len(SOURCE_ORDER)), dtype=np.float64)
    bias = np.zeros(len(SOURCE_ORDER), dtype=np.float64)
    positive = targets.sum(axis=0)
    negative = rows - positive
    positive_weight = negative / np.maximum(positive, 1.0)
    sample_weights = np.where(targets > 0.5, positive_weight, 1.0)
    sample_weights /= sample_weights.mean(axis=0, keepdims=True)

    first_w = np.zeros_like(weights)
    second_w = np.zeros_like(weights)
    first_b = np.zeros_like(bias)
    second_b = np.zeros_like(bias)
    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    losses: list[float] = []
    for epoch in range(1, epochs + 1):
        probabilities = _sigmoid(features @ weights + bias)
        residual = (probabilities - targets) * sample_weights
        grad_w = (features.T @ residual) / rows + l2 * weights
        grad_b = residual.mean(axis=0)

        first_w = beta1 * first_w + (1.0 - beta1) * grad_w
        second_w = beta2 * second_w + (1.0 - beta2) * np.square(grad_w)
        first_b = beta1 * first_b + (1.0 - beta1) * grad_b
        second_b = beta2 * second_b + (1.0 - beta2) * np.square(grad_b)
        correction1 = 1.0 - beta1 ** epoch
        correction2 = 1.0 - beta2 ** epoch
        weights -= learning_rate * (
            first_w / correction1
        ) / (np.sqrt(second_w / correction2) + epsilon)
        bias -= learning_rate * (
            first_b / correction1
        ) / (np.sqrt(second_b / correction2) + epsilon)

        if epoch == 1 or epoch % 100 == 0 or epoch == epochs:
            probabilities = np.clip(probabilities, 1e-9, 1.0 - 1e-9)
            bce = -(
                sample_weights * (
                    targets * np.log(probabilities)
                    + (1.0 - targets) * np.log(1.0 - probabilities)
                )
            ).mean()
            loss = float(bce + 0.5 * l2 * np.square(weights).sum())
            losses.append(loss)
    return weights, bias, losses


def _f1(target: np.ndarray, predicted: np.ndarray) -> tuple[float, float, float]:
    true_positive = int(np.logical_and(target, predicted).sum())
    false_positive = int(np.logical_and(~target, predicted).sum())
    false_negative = int(np.logical_and(target, ~predicted).sum())
    precision = true_positive / max(true_positive + false_positive, 1)
    recall = true_positive / max(true_positive + false_negative, 1)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    return precision, recall, f1


def evaluate(
    cases: Sequence[IntentRouteCase],
    targets: np.ndarray,
    probabilities: np.ndarray,
    thresholds: Sequence[float],
) -> dict[str, Any]:
    predicted = probabilities >= np.asarray(thresholds)
    exact = np.all(predicted == (targets > 0.5), axis=1)
    by_group: dict[str, list[bool]] = defaultdict(list)
    for case, correct in zip(cases, exact):
        by_group[case.group].append(bool(correct))
    source_metrics = {}
    for index, source in enumerate(SOURCE_ORDER):
        precision, recall, f1 = _f1(
            targets[:, index] > 0.5, predicted[:, index]
        )
        source_metrics[source] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    group_exact = {
        group: round(sum(values) / len(values), 6)
        for group, values in by_group.items()
    }
    return {
        "count": len(cases),
        "exact_match": round(float(exact.mean()), 6),
        "macro_group_exact_match": round(
            sum(group_exact.values()) / len(group_exact), 6
        ),
        "macro_source_f1": round(
            sum(item["f1"] for item in source_metrics.values())
            / len(source_metrics), 6
        ),
        "source": source_metrics,
        "group_exact_match": group_exact,
    }


def calibrate_thresholds(
    cases: Sequence[IntentRouteCase],
    targets: np.ndarray,
    probabilities: np.ndarray,
) -> tuple[tuple[float, ...], dict[str, Any]]:
    """Jointly optimize thresholds, prioritizing balanced group exact-match."""
    grid = np.round(np.arange(0.20, 0.901, 0.01), 2)
    truth = targets > 0.5
    group_indices: dict[str, np.ndarray] = {}
    for group in {case.group for case in cases}:
        group_indices[group] = np.asarray([
            index for index, case in enumerate(cases) if case.group == group
        ])

    best_thresholds: tuple[float, ...] | None = None
    best_score: tuple[float, ...] | None = None
    for academic_threshold in grid:
        academic = probabilities[:, 0] >= academic_threshold
        for patent_threshold in grid:
            patent = probabilities[:, 1] >= patent_threshold
            first_two = np.column_stack((academic, patent))
            first_two_correct = np.all(first_two == truth[:, :2], axis=1)
            for legal_threshold in grid:
                legal = probabilities[:, 2] >= legal_threshold
                exact = np.logical_and(first_two_correct, legal == truth[:, 2])
                group_exact = [
                    float(exact[indices].mean())
                    for indices in group_indices.values()
                ]
                predicted = np.column_stack((academic, patent, legal))
                source_f1 = [
                    _f1(truth[:, index], predicted[:, index])[2]
                    for index in range(len(SOURCE_ORDER))
                ]
                legal_recall = _f1(truth[:, 2], legal)[1]
                # Rounded leading metrics make tie-breaking deterministic and
                # prefer the less permissive solution when quality is equal.
                score = (
                    round(sum(group_exact) / len(group_exact), 9),
                    round(float(exact.mean()), 9),
                    round(sum(source_f1) / len(source_f1), 9),
                    round(legal_recall, 9),
                    academic_threshold + patent_threshold + legal_threshold,
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best_thresholds = (
                        float(academic_threshold),
                        float(patent_threshold),
                        float(legal_threshold),
                    )
    if best_thresholds is None:  # pragma: no cover - non-empty fixed grid
        raise RuntimeError("threshold calibration produced no candidate")
    return best_thresholds, evaluate(
        cases, targets, probabilities, best_thresholds
    )


def _corpus_hash(cases: Iterable[IntentRouteCase]) -> str:
    canonical = "\n".join(
        json.dumps({
            "id": case.case_id,
            "split": case.split,
            "query": case.query,
            "source_types": case.source_types,
        }, ensure_ascii=False, sort_keys=True)
        for case in cases
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _round_matrix(values: np.ndarray) -> list[list[float]]:
    return [[round(float(value), 9) for value in row] for row in values]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--embedding-cache", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--epochs", type=int, default=1200)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--l2", type=float, default=0.0003)
    args = parser.parse_args()

    env = _load_local_env()
    api_key = env.get("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise SystemExit("SILICONFLOW_API_KEY is required")
    base_url = args.base_url or env.get("SILICONFLOW_BASE_URL", DEFAULT_BASE_URL)
    train = training_cases()
    validation = validation_cases()
    cases = (*train, *validation)
    print(f"train={len(train)} {group_counts(train)}")
    print(f"validation={len(validation)} {group_counts(validation)}")

    features = fetch_embeddings(
        cases,
        api_key=api_key,
        base_url=base_url,
        model=args.model,
        batch_size=args.batch_size,
        timeout=args.timeout,
        cache_path=args.embedding_cache,
    )
    train_features = features[:len(train)]
    validation_features = features[len(train):]
    train_targets = _targets(train)
    validation_targets = _targets(validation)
    weights, bias, losses = train_logistic_heads(
        train_features,
        train_targets,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        l2=args.l2,
    )
    train_probabilities = _sigmoid(train_features @ weights + bias)
    validation_probabilities = _sigmoid(validation_features @ weights + bias)
    thresholds, validation_metrics = calibrate_thresholds(
        validation, validation_targets, validation_probabilities
    )
    train_metrics = evaluate(
        train, train_targets, train_probabilities, thresholds
    )

    artifact = {
        "artifact_version": 1,
        "classifier": "independent_logistic_heads",
        "embedding_model": args.model,
        "embedding_dimension": int(features.shape[1]),
        "query_prefix": QUERY_PREFIX,
        "source_order": list(SOURCE_ORDER),
        "thresholds": {
            source: threshold
            for source, threshold in zip(SOURCE_ORDER, thresholds)
        },
        "weights": {
            source: _round_matrix(weights.T)[index]
            for index, source in enumerate(SOURCE_ORDER)
        },
        "bias": {
            source: round(float(bias[index]), 9)
            for index, source in enumerate(SOURCE_ORDER)
        },
        "training": {
            "corpus_sha256": _corpus_hash(cases),
            "split_counts": {
                "train": len(train),
                "validation": len(validation),
            },
            "group_counts": {
                "train": group_counts(train),
                "validation": group_counts(validation),
            },
            "epochs": args.epochs,
            "learning_rate": args.learning_rate,
            "l2": args.l2,
            "class_balanced": True,
            "loss_trace": [round(value, 8) for value in losses],
            "threshold_objective": [
                "macro_group_exact_match",
                "exact_match",
                "macro_source_f1",
                "legal_recall",
                "higher_threshold_sum",
            ],
            "metrics": {
                "train": train_metrics,
                "validation": validation_metrics,
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"thresholds={dict(zip(SOURCE_ORDER, thresholds))}")
    print(json.dumps({
        "train": train_metrics,
        "validation": validation_metrics,
    }, ensure_ascii=False, indent=2))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
