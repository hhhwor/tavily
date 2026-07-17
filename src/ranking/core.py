"""Domain-independent ranking algorithm and request context."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generic, List, Optional, Sequence, Tuple, TypeVar

from src.models import SearchResult
from src.pipeline.ranking_options import RankingProfile, ThresholdMode
from src.ranking.ports import TextScorer, clamp01

T = TypeVar("T", bound=SearchResult)

TEXT = "text"
PRIOR = "prior"
CITATIONS = "citations"
FRESHNESS = "freshness"
VENUE = "venue"
OA = "oa"
SOURCE_SCORE = "source_score"
STATUS = "status"

_DATE_PATTERNS = [
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})"),
    re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
]
_RECENT_QUERY_RE = re.compile(
    r"\b(latest|recent|newest|state of the art|sota|202\d)\b|最新|最近|近年|近期|202\d"
)
_FOUNDATIONAL_QUERY_RE = re.compile(
    r"\b(survey|review|benchmark|foundation|foundational|seminal)\b|综述|综述性|基准|经典"
)


def parse_date_days_ago(
    date_str: str, reference_time: datetime | None = None
) -> Optional[int]:
    if not date_str:
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(date_str)
        if match:
            try:
                date = datetime(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                    tzinfo=timezone.utc,
                )
                now = reference_time or datetime.now(timezone.utc)
                return max(0, (now - date).days)
            except (TypeError, ValueError):
                continue
    return None


@dataclass(frozen=True)
class RerankContext:
    time_sensitive: bool = False
    wants_recent: bool = False
    wants_foundational: bool = False
    reference_time: datetime | None = None


def build_rerank_context(
    query: str,
    time_sensitive: bool = False,
    reference_time: datetime | None = None,
) -> RerankContext:
    normalized = (query or "").lower()
    return RerankContext(
        time_sensitive=time_sensitive,
        wants_recent=bool(_RECENT_QUERY_RE.search(normalized)),
        wants_foundational=bool(_FOUNDATIONAL_QUERY_RE.search(normalized)),
        reference_time=reference_time,
    )


@dataclass
class Scored:
    key: str
    text: float
    features: Dict[str, float]
    passed_threshold: bool = False
    final: float = 0.0


FeatureFn = Callable[[T, int, Sequence[T], RerankContext], float]


@dataclass
class DomainConfig(Generic[T]):
    name: str
    key_fn: Callable[[T, int], str]
    compress_fn: Callable[[T], str]
    feature_fns: Dict[str, FeatureFn[T]]
    weight_fn: Callable[[str, RerankContext], Dict[str, float]]
    profile: RankingProfile = "quality"
    threshold: float = 0.3
    threshold_mode: ThresholdMode = "prefer"
    score_text: bool = True
    max_docs: Optional[int] = None
    prepare_fn: Optional[Callable[[List[T]], List[T]]] = None
    pass_bonus: float = 0.0
    tiebreaker_fn: Optional[Callable[[T, int], Tuple[Any, ...]]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def normalize_list(
    values: Sequence[Optional[float]], default: float = 0.0
) -> List[float]:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return [default for _ in values]
    low, high = min(numbers), max(numbers)
    if math.isclose(low, high):
        flat = 0.5 if high > 0 else default
        return [flat if value is not None else default for value in values]
    return [
        ((float(value) - low) / (high - low)) if value is not None else default
        for value in values
    ]


def normalized_feature(
    pool: Sequence[T],
    index: int,
    value_fn: Callable[[T], Optional[float]],
    default: float = 0.0,
) -> float:
    return normalize_list(
        [value_fn(candidate) for candidate in pool], default=default
    )[index]


def _validate_weights(config: DomainConfig[Any], weights: Dict[str, float]) -> None:
    expected = set(config.feature_fns) | {TEXT}
    if expected != set(weights):
        raise ValueError(
            f"{config.name} weights mismatch: expected={sorted(expected)} "
            f"actual={sorted(weights)}"
        )


def rerank_domain(
    query: str,
    candidates: List[T],
    config: DomainConfig[T],
    text_scorer: TextScorer,
    ctx: RerankContext,
    top_k: int,
) -> List[T]:
    """Rank immutable candidate copies through one shared policy algorithm."""
    if not candidates:
        return []

    working = [candidate.model_copy(deep=True) for candidate in candidates]
    prepared = config.prepare_fn(working) if config.prepare_fn else working
    pool = prepared[: config.max_docs] if config.max_docs else prepared
    if not pool:
        return []

    scorer_available = config.score_text and text_scorer.supports_text_scoring
    if scorer_available:
        text_scores = list(
            text_scorer.score(query, [config.compress_fn(candidate) for candidate in pool])
        )
        if len(text_scores) != len(pool):
            raise ValueError(
                f"{config.name} scorer 返回 {len(text_scores)} 个分数，期望 {len(pool)} 个"
            )
    else:
        text_scores = [0.0 for _ in pool]

    threshold_mode: ThresholdMode = config.threshold_mode
    if config.threshold <= 0 or not scorer_available:
        threshold_mode = "off"

    weights = config.weight_fn(query, ctx)
    _validate_weights(config, weights)
    scored: List[Scored] = []
    for index, candidate in enumerate(pool):
        text_score = clamp01(text_scores[index])
        features = {
            name: clamp01(feature(candidate, index, pool, ctx))
            for name, feature in config.feature_fns.items()
        }
        passed = text_score >= config.threshold
        final = weights[TEXT] * text_score + sum(
            weights[name] * value for name, value in features.items()
        )
        if threshold_mode != "off" and config.pass_bonus and passed:
            final += config.pass_bonus
        scored.append(
            Scored(
                key=config.key_fn(candidate, index),
                text=text_score,
                features=features,
                passed_threshold=passed,
                final=clamp01(final),
            )
        )

    for candidate, score in zip(pool, scored):
        candidate.rerank_score = score.final

    def sort_key(index: int) -> Tuple[Any, ...]:
        tie = config.tiebreaker_fn(pool[index], index) if config.tiebreaker_fn else (index,)
        return (-scored[index].final, *tie)

    base_order = sorted(range(len(pool)), key=sort_key)
    if threshold_mode == "strict":
        order = [index for index in base_order if scored[index].passed_threshold]
    elif threshold_mode == "prefer":
        passed = [index for index in base_order if scored[index].passed_threshold]
        failed = [index for index in base_order if not scored[index].passed_threshold]
        order = passed + failed
    else:
        order = base_order
    return [pool[index] for index in order][:top_k]
