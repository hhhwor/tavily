"""领域重排序:可选文本 scorer、结构化信号融合与显式阈值策略。

段落级重排:
  1. 文档先切 chunk(src.pipeline.chunk)
  2. 逐块与 query 组 pair 交给 cross-encoder 打分
  3. 每文档取 chunk 最高分(max-pooling)
  4. sigmoid 归一化到 0-1
  5. 按 off / prefer / strict 应用融合前文本分阈值

Profile:quality 融合文本与领域信号;semantic 只用文本;fast 不调用文本模型。
回退:scorer 不可用时自动降级为 NoOpReranker,保证管线始终可跑。
"""
from __future__ import annotations

import math
import re as _re
import requests as _requests
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generic, List, Optional, Sequence, Tuple, TypeVar

from src.models import AcademicResult, PatentResult, SearchResult
from src.pipeline.chunk import chunk_text
from src.pipeline.dedup import normalize_url
from src.pipeline.fusion import rrf_fuse
from src.pipeline.ranking_options import (
    RankingProfile,
    ThresholdMode,
    parse_ranking_profile,
    parse_threshold_mode,
)


T = TypeVar("T", bound=SearchResult)

TEXT = "text"
PRIOR = "prior"
CITATIONS = "citations"
FRESHNESS = "freshness"
VENUE = "venue"
OA = "oa"
SOURCE_SCORE = "source_score"
STATUS = "status"


class Reranker(ABC):
    name: str = "base"
    supports_text_scoring: bool = True

    @abstractmethod
    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        raise NotImplementedError

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        """按输入顺序返回文本相关性分;兼容旧 reranker 的默认实现。"""
        pseudo = [
            SearchResult(url=f"__text_{i}", title="", content=text or "")
            for i, text in enumerate(texts)
        ]
        self.rerank(query, pseudo, len(pseudo))
        return [clamp01(r.rerank_score or 0.0) for r in pseudo]


class NoOpReranker(Reranker):
    """不重排:按来源原始分(若有)降序,否则保持原顺序。"""

    name = "noop"
    supports_text_scoring = False

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        ordered = sorted(
            results, key=lambda r: (r.score is not None, r.score or 0.0), reverse=True
        )
        return ordered[:top_k]

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return [0.0 for _ in texts]


def sigmoid_normalize(scores: List[float], temperature: float = 1.0) -> List[float]:
    """将任意范围的分数通过 sigmoid 归一化到 0-1。

    temperature > 1 使分布更尖锐(拉开差距),< 1 更平缓。
    """
    return [1.0 / (1.0 + math.exp(-s * temperature)) for s in scores]


def clamp01(value: Optional[float], default: float = 0.0) -> float:
    """将分数压到 0-1,处理 None/NaN/无穷大。"""
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return max(0.0, min(1.0, v))


def _normalize_list(values: Sequence[Optional[float]], default: float = 0.0) -> List[float]:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return [default for _ in values]
    lo, hi = min(nums), max(nums)
    if math.isclose(lo, hi):
        flat = 0.5 if hi > 0 else default
        return [flat if v is not None else default for v in values]
    return [
        ((float(v) - lo) / (hi - lo)) if v is not None else default
        for v in values
    ]


class FlashRankReranker(Reranker):
    """FlashRank cross-encoder 段落级重排。"""

    name = "flashrank"

    def __init__(
        self,
        model_name: str,
        cache_dir: str,
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ):
        from flashrank import Ranker  # 延迟导入

        self._ranker = Ranker(model_name=model_name, cache_dir=cache_dir)
        self._lock = threading.Lock()
        self.name = f"flashrank:{model_name}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        from flashrank import RerankRequest

        if not texts:
            return []

        pairs: List[tuple[int, str]] = []
        for i, text in enumerate(texts):
            for c in chunk_text(text or "", self._chunk_max_chars, self._chunk_overlap):
                pairs.append((i, c))

        if not pairs:
            return [0.0 for _ in texts]

        passages = [{"id": j, "text": t} for j, (_, t) in enumerate(pairs)]
        with self._lock:
            scored = self._ranker.rerank(RerankRequest(query=query, passages=passages))

        doc_scores: dict[int, float] = {}
        for item in scored:
            doc_idx = pairs[item["id"]][0]
            s = float(item["score"])
            if doc_idx not in doc_scores or s > doc_scores[doc_idx]:
                doc_scores[doc_idx] = s

        raw = [doc_scores.get(i, 0.0) for i in range(len(texts))]
        return sigmoid_normalize(raw)

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        if not results:
            return []

        normed = self.score(query, [r.text_for_rerank() for r in results])
        for r, s in zip(results, normed):
            r.rerank_score = s

        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return ranked[:top_k]


class BGEReranker(Reranker):
    """BGE-Reranker-v2-m3 cross-encoder 段落级重排(多语言,中文强;需 torch)。"""

    name = "bge"

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        max_length: int = 512,
        device: Optional[str] = None,
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ):
        from sentence_transformers import CrossEncoder  # 延迟导入

        kwargs = {"max_length": max_length}
        if device:
            kwargs["device"] = device
        self._model = CrossEncoder(model_name, **kwargs)
        self._lock = threading.Lock()
        self.name = f"bge:{model_name.split('/')[-1]}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        if not texts:
            return []

        pairs: List[tuple[int, str]] = []
        for i, text in enumerate(texts):
            for c in chunk_text(text or "", self._chunk_max_chars, self._chunk_overlap):
                pairs.append((i, c))

        if not pairs:
            return [0.0 for _ in texts]

        predict_pairs = [(query, t) for _, t in pairs]
        with self._lock:
            scores = self._model.predict(predict_pairs)

        doc_scores: dict[int, float] = {}
        for (doc_idx, _), s in zip(pairs, scores):
            s = float(s)
            if doc_idx not in doc_scores or s > doc_scores[doc_idx]:
                doc_scores[doc_idx] = s

        raw = [doc_scores.get(i, 0.0) for i in range(len(texts))]
        return sigmoid_normalize(raw)

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        if not results:
            return []

        normed = self.score(query, [r.text_for_rerank() for r in results])
        for r, s in zip(results, normed):
            r.rerank_score = s

        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return ranked[:top_k]


class SiliconFlowReranker(Reranker):
    """SiliconFlow (硅基流动) 云端 rerank API。

    使用云端 cross-encoder,无需本地 GPU。当前实现不再假设 API 存在 25 条硬上限,
    而是将本地切分后的 passages 一次性发送;API 返回的 relevance_score 已在 0-1 范围,
    无需 sigmoid。
    """

    name = "siliconflow"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "BAAI/bge-reranker-v2-m3",
        chunk_max_chars: int = 400,
        chunk_overlap: int = 50,
    ):
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/rerank"
        self._model = model
        self.name = f"siliconflow:{model.split('/')[-1]}"
        self._chunk_max_chars = chunk_max_chars
        self._chunk_overlap = chunk_overlap

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        if not texts:
            return []

        pairs: List[tuple[int, str]] = []
        for i, text in enumerate(texts):
            for c in chunk_text(text or "", self._chunk_max_chars, self._chunk_overlap):
                pairs.append((i, c))

        if not pairs:
            return [0.0 for _ in texts]

        doc_scores: dict[int, float] = {}
        documents = [t for _, t in pairs]

        resp = _requests.post(
            self._url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
                "return_documents": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("results", []):
            doc_idx = pairs[item["index"]][0]
            s = float(item["relevance_score"])
            if doc_idx not in doc_scores or s > doc_scores[doc_idx]:
                doc_scores[doc_idx] = s

        return [clamp01(doc_scores.get(i, 0.0)) for i in range(len(texts))]

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        if not results:
            return []

        scores = self.score(query, [r.text_for_rerank() for r in results])
        for i, r in enumerate(results):
            r.rerank_score = scores[i] if i < len(scores) else 0.0

        ranked = sorted(results, key=lambda r: r.rerank_score or 0.0, reverse=True)
        return ranked[:top_k]


class ThresholdReranker(Reranker):
    """包装器:在内部 reranker 基础上增加 sigmoid 阈值过滤。"""

    def __init__(self, inner: Reranker, threshold: float = 0.3):
        self._inner = inner
        self._threshold = threshold
        self.name = inner.name
        self.supports_text_scoring = inner.supports_text_scoring

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        ranked = self._inner.rerank(query, results, top_k)
        if self._threshold <= 0:
            return ranked
        return [r for r in ranked if (r.rerank_score or 0) >= self._threshold][:top_k]

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return self._inner.score(query, texts)


# --- 日期解析 ---

_DATE_PATTERNS = [
    _re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),   # 2026-06-05
    _re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})"),   # 2026.06.05
    _re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"),     # 2026/06/05
    _re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),  # 2026年6月5日
]


def _parse_date_days_ago(date_str: str) -> Optional[int]:
    """解析日期字符串,返回距今天数。失败返回 None。"""
    if not date_str:
        return None
    for pat in _DATE_PATTERNS:
        m = pat.search(date_str)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                return max(0, (datetime.now(timezone.utc) - dt).days)
            except (ValueError, TypeError):
                continue
    return None


# --- 来源权威度默认权重 ---

_DEFAULT_AUTHORITY = {
    "serpapi": 0.95,   # Google 索引
    "tencent": 0.90,   # 腾讯自有索引
    "baidu": 0.85,     # 百度自有索引
}

_RECENT_QUERY_RE = _re.compile(
    r"\b(latest|recent|newest|state of the art|sota|202\d)\b|最新|最近|近年|近期|202\d"
)
_FOUNDATIONAL_QUERY_RE = _re.compile(
    r"\b(survey|review|benchmark|foundation|foundational|seminal)\b|综述|综述性|基准|经典"
)


@dataclass(frozen=True)
class RerankContext:
    """单次请求的不可变重排上下文,避免在共享 reranker 上写请求状态。"""

    time_sensitive: bool = False
    wants_recent: bool = False
    wants_foundational: bool = False


def build_rerank_context(query: str, time_sensitive: bool = False) -> RerankContext:
    q = (query or "").lower()
    return RerankContext(
        time_sensitive=time_sensitive,
        wants_recent=bool(_RECENT_QUERY_RE.search(q)),
        wants_foundational=bool(_FOUNDATIONAL_QUERY_RE.search(q)),
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
    name_prefix: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


def _validate_weights(config: DomainConfig[Any], weights: Dict[str, float]) -> None:
    expected = set(config.feature_fns) | {TEXT}
    actual = set(weights)
    if expected != actual:
        raise ValueError(
            f"{config.name} weights mismatch: expected={sorted(expected)} actual={sorted(actual)}"
        )


def rerank_domain(
    query: str,
    candidates: List[T],
    config: DomainConfig[T],
    text_scorer: Reranker,
    ctx: RerankContext,
    top_k: int,
) -> List[T]:
    """组合式领域重排:候选准备、文本打分、领域特征融合共用一份实现。"""
    if not candidates:
        return []

    prepared = config.prepare_fn(list(candidates)) if config.prepare_fn else list(candidates)
    pool = prepared[: config.max_docs] if config.max_docs else prepared
    if not pool:
        return []

    scorer_available = config.score_text and text_scorer.supports_text_scoring
    if scorer_available:
        texts = [config.compress_fn(c) for c in pool]
        text_scores = list(text_scorer.score(query, texts))
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
    for i, c in enumerate(pool):
        text = clamp01(text_scores[i])
        features = {
            name: clamp01(fn(c, i, pool, ctx))
            for name, fn in config.feature_fns.items()
        }
        passed_threshold = text >= config.threshold
        final = weights[TEXT] * text + sum(weights[name] * features[name] for name in features)
        if threshold_mode != "off" and config.pass_bonus and passed_threshold:
            final += config.pass_bonus
        scored.append(
            Scored(
                key=config.key_fn(c, i),
                text=text,
                features=features,
                passed_threshold=passed_threshold,
                final=clamp01(final),
            )
        )

    for c, s in zip(pool, scored):
        c.rerank_score = s.final

    def sort_key(i: int) -> Tuple[Any, ...]:
        tiebreaker = config.tiebreaker_fn(pool[i], i) if config.tiebreaker_fn else (i,)
        return (-scored[i].final, *tiebreaker)

    base_order = sorted(range(len(pool)), key=sort_key)
    if threshold_mode == "strict":
        order = [i for i in base_order if scored[i].passed_threshold]
    elif threshold_mode == "prefer":
        passed = [i for i in base_order if scored[i].passed_threshold]
        failed = [i for i in base_order if not scored[i].passed_threshold]
        order = passed + failed
    else:
        order = base_order
    return [pool[i] for i in order][:top_k]


def _rank_fallback(n: int) -> List[float]:
    if n <= 0:
        return []
    vals = [1.0 / (1.0 + i) for i in range(n)]
    lo, hi = vals[-1], vals[0]
    if math.isclose(lo, hi):
        return [1.0 for _ in vals]
    return [(v - lo) / (hi - lo) for v in vals]


def _feature_normalized(
    pool: Sequence[T],
    idx: int,
    value_fn: Callable[[T], Optional[float]],
    default: float = 0.0,
) -> float:
    return _normalize_list([value_fn(item) for item in pool], default=default)[idx]


def _web_key(result: SearchResult, idx: int) -> str:
    return normalize_url(result.url) or result.url or f"web:{idx}"


def _stable_rrf_prepare(results: List[SearchResult]) -> List[SearchResult]:
    groups: dict[str, list] = {}
    order: List[str] = []
    for idx, r in enumerate(results):
        key = normalize_url(r.url) or r.url or f"web:{idx}"
        rank = r.provider_rank if r.provider_rank is not None else 0
        contrib = 1.0 / (60 + rank + 1)
        if key in groups:
            rep, sc, first_idx = groups[key]
            keep, other = (rep, r) if len(rep.content) >= len(r.content) else (r, rep)
            if other.source and other.source not in keep.source:
                keep.source = f"{keep.source}+{other.source}" if keep.source else other.source
            groups[key] = [keep, sc + contrib, first_idx]
        else:
            groups[key] = [r, contrib, idx]
            order.append(key)

    prepared: List[SearchResult] = []
    for key in order:
        rep, sc, first_idx = groups[key]
        rep.raw["_rrf_prior"] = sc
        rep.raw["_rrf_first_idx"] = first_idx
        prepared.append(rep)

    prepared.sort(
        key=lambda r: (
            -float(r.raw.get("_rrf_prior", 0.0)),
            r.provider_rank if r.provider_rank is not None else 10 ** 9,
            r.url,
        )
    )
    return prepared


def _compress_web_text(result: SearchResult, max_chars: int = 320) -> str:
    parts: List[str] = []
    title = (result.title or "").strip()
    snippet = (result.snippet or "").strip()
    content = (result.content or "").strip()
    if title:
        parts.append(title)
    if snippet:
        parts.append(snippet)
    prefix = content[: max(len(snippet) + 80, 120)] if snippet else content[:120]
    if content and (not snippet or snippet not in prefix):
        parts.append(content)
    return "\n".join(parts).strip()[:max_chars]


def _web_prior_feature(
    result: SearchResult,
    idx: int,
    pool: Sequence[SearchResult],
    ctx: RerankContext,
) -> float:
    return _feature_normalized(
        pool,
        idx,
        lambda r: float(r.raw.get("_rrf_prior", 0.0)),
        default=0.0,
    )


def _web_weights(query: str, ctx: RerankContext) -> Dict[str, float]:
    return {TEXT: 0.85, PRIOR: 0.15}


def _web_tiebreaker(result: SearchResult, idx: int) -> Tuple[Any, ...]:
    return (result.provider_rank if result.provider_rank is not None else 10 ** 9, result.url)


def build_web_config(
    threshold: float = 0.3,
    max_chars: int = 320,
    text_weight: float = 0.85,
    rrf_weight: float = 0.15,
    pass_bonus: float = 0.02,
    profile: str = "quality",
    threshold_mode: str = "prefer",
) -> DomainConfig[SearchResult]:
    effective_profile = parse_ranking_profile(profile)
    effective_threshold_mode = parse_threshold_mode(threshold_mode)

    def weights(query: str, ctx: RerankContext) -> Dict[str, float]:
        if effective_profile == "semantic":
            return {TEXT: 1.0, PRIOR: 0.0}
        if effective_profile == "fast":
            return {TEXT: 0.0, PRIOR: 1.0}
        return {TEXT: text_weight, PRIOR: rrf_weight}

    def tiebreaker(result: SearchResult, idx: int) -> Tuple[Any, ...]:
        if effective_profile == "semantic":
            return (_web_key(result, idx), idx)
        return _web_tiebreaker(result, idx)

    return DomainConfig(
        name="web",
        key_fn=_web_key,
        compress_fn=lambda r: _compress_web_text(r, max_chars=max_chars),
        feature_fns={PRIOR: _web_prior_feature},
        weight_fn=weights,
        profile=effective_profile,
        threshold=threshold,
        threshold_mode=effective_threshold_mode,
        score_text=effective_profile != "fast",
        prepare_fn=_stable_rrf_prepare,
        pass_bonus=pass_bonus,
        tiebreaker_fn=tiebreaker,
    )


def _academic_key(result: AcademicResult, idx: int) -> str:
    return result.doi or result.url or f"{result.title}|{result.year}|{idx}"


def _compress_academic_text(result: AcademicResult, max_chars: int = 480) -> str:
    title = (result.title or "").strip()
    body = (result.content or result.snippet or "").strip()
    if not title:
        return body[:max_chars]
    available = max(0, max_chars - len(title) - 1)
    return f"{title}\n{body[:available]}".strip()


def _academic_citations_feature(
    result: AcademicResult,
    idx: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    return _feature_normalized(
        pool,
        idx,
        lambda p: math.log1p(max(0, p.citations)),
        default=0.0,
    )


def _academic_source_score_feature(
    result: AcademicResult,
    idx: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    return _feature_normalized(pool, idx, lambda p: p.score, default=0.0)


def _academic_freshness_feature(
    result: AcademicResult,
    idx: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    days_ago = _parse_date_days_ago(result.date)
    if days_ago is None and result.year:
        try:
            dt = datetime(int(result.year), 1, 1, tzinfo=timezone.utc)
            days_ago = max(0, (datetime.now(timezone.utc) - dt).days)
        except (TypeError, ValueError):
            days_ago = None
    if days_ago is None:
        return 0.4 if ctx.wants_recent else 0.5
    if ctx.wants_recent:
        return 1.0 / (1.0 + days_ago / 365.0)
    # Scholar-like default: publication year is a weak prior unless the query asks for recent work.
    return max(0.0, 1.0 - days_ago / (365.0 * 20.0))


def _venue_value(venue: str) -> float:
    if not venue:
        return 0.0
    lower = venue.lower()
    if "arxiv" in lower or "biorxiv" in lower or "medrxiv" in lower or "ssrn" in lower:
        return 0.35
    return 1.0


def _academic_venue_feature(
    result: AcademicResult,
    idx: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    return _venue_value(result.venue or result.site)


def _academic_oa_feature(
    result: AcademicResult,
    idx: int,
    pool: Sequence[AcademicResult],
    ctx: RerankContext,
) -> float:
    if result.oa_pdf_url:
        return 1.0
    if result.oa_landing_url or result.is_oa:
        return 0.7
    return 0.0


def _academic_weights(query: str, ctx: RerankContext) -> Dict[str, float]:
    if ctx.wants_recent and ctx.wants_foundational:
        return {TEXT: 0.66, CITATIONS: 0.16, FRESHNESS: 0.12, VENUE: 0.04, OA: 0.02}
    if ctx.wants_recent:
        return {TEXT: 0.68, CITATIONS: 0.08, FRESHNESS: 0.18, VENUE: 0.04, OA: 0.02}
    if ctx.wants_foundational:
        return {TEXT: 0.66, CITATIONS: 0.26, FRESHNESS: 0.01, VENUE: 0.05, OA: 0.02}
    return {TEXT: 0.70, CITATIONS: 0.20, FRESHNESS: 0.02, VENUE: 0.05, OA: 0.03}


def _academic_tiebreaker(result: AcademicResult, idx: int) -> Tuple[Any, ...]:
    return (
        -(float(result.score) if result.score is not None else 0.0),
        -max(0, result.citations),
        -(result.year or 0),
        result.title,
        idx,
    )


def build_academic_config(
    threshold: float = 0.3,
    max_docs: int = 25,
    max_chars: int = 480,
    profile: str = "quality",
    threshold_mode: str = "prefer",
) -> DomainConfig[AcademicResult]:
    effective_profile = parse_ranking_profile(profile)
    effective_threshold_mode = parse_threshold_mode(threshold_mode)

    def weights(query: str, ctx: RerankContext) -> Dict[str, float]:
        if effective_profile == "semantic":
            return {
                TEXT: 1.0, SOURCE_SCORE: 0.0, CITATIONS: 0.0,
                FRESHNESS: 0.0, VENUE: 0.0, OA: 0.0,
            }
        if effective_profile == "fast":
            return {
                TEXT: 0.0, SOURCE_SCORE: 1.0, CITATIONS: 0.0,
                FRESHNESS: 0.0, VENUE: 0.0, OA: 0.0,
            }
        return {SOURCE_SCORE: 0.0, **_academic_weights(query, ctx)}

    def tiebreaker(result: AcademicResult, idx: int) -> Tuple[Any, ...]:
        if effective_profile == "semantic":
            return (_academic_key(result, idx), idx)
        if effective_profile == "fast":
            return (idx,)
        return _academic_tiebreaker(result, idx)

    return DomainConfig(
        name="academic",
        key_fn=_academic_key,
        compress_fn=lambda p: _compress_academic_text(p, max_chars=max_chars),
        feature_fns={
            SOURCE_SCORE: _academic_source_score_feature,
            CITATIONS: _academic_citations_feature,
            FRESHNESS: _academic_freshness_feature,
            VENUE: _academic_venue_feature,
            OA: _academic_oa_feature,
        },
        weight_fn=weights,
        profile=effective_profile,
        threshold=threshold,
        threshold_mode=effective_threshold_mode,
        score_text=effective_profile != "fast",
        max_docs=max_docs,
        tiebreaker_fn=tiebreaker,
    )


def _patent_key(result: PatentResult, idx: int) -> str:
    return (
        result.publication_number
        or result.url
        or f"{result.title}|{result.application_number}|{idx}"
    )


def _compress_patent_text(result: PatentResult, max_chars: int = 520) -> str:
    parts = []
    if result.title:
        parts.append(result.title.strip())
    body = (result.content or result.snippet or "").strip()
    if body:
        parts.append(f"摘要: {body}")
    applicants = [a for a in result.applicant[:3] if a]
    if applicants:
        parts.append(f"申请人: {'; '.join(applicants)}")
    classification = result.ipc_main or result.cpc_main
    if classification:
        parts.append(f"分类: {classification}")
    if result.publication_number:
        parts.append(f"公开号: {result.publication_number}")
    return "\n".join(parts).strip()[:max_chars]


def _patent_source_score_feature(
    result: PatentResult,
    idx: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    return _feature_normalized(pool, idx, lambda p: p.score, default=0.0)


def _patent_freshness_feature(
    result: PatentResult,
    idx: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    days_ago = _parse_date_days_ago(result.publication_date or result.application_date or result.date)
    if days_ago is None:
        return 0.4 if ctx.wants_recent else 0.5
    if ctx.wants_recent or ctx.time_sensitive:
        return 1.0 / (1.0 + days_ago / 365.0)
    return max(0.0, 1.0 - days_ago / (365.0 * 10.0))


def _patent_citations_feature(
    result: PatentResult,
    idx: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    return _feature_normalized(
        pool,
        idx,
        lambda p: math.log1p(max(0, p.citation_count)),
        default=0.0,
    )


def _status_value(status: str) -> float:
    if not status:
        return 0.5
    lower = status.lower()
    if any(x in lower for x in ("active", "granted", "grant", "pending", "published", "alive")):
        return 1.0
    if any(x in lower for x in ("expired", "withdrawn", "abandoned", "lapsed", "dead")):
        return 0.2
    return 0.5


def _patent_status_feature(
    result: PatentResult,
    idx: int,
    pool: Sequence[PatentResult],
    ctx: RerankContext,
) -> float:
    return _status_value(result.status)


def _patent_weights(query: str, ctx: RerankContext) -> Dict[str, float]:
    if ctx.wants_recent or ctx.time_sensitive:
        return {TEXT: 0.70, SOURCE_SCORE: 0.10, FRESHNESS: 0.12, CITATIONS: 0.04, STATUS: 0.04}
    return {TEXT: 0.72, SOURCE_SCORE: 0.12, FRESHNESS: 0.06, CITATIONS: 0.06, STATUS: 0.04}


def _patent_tiebreaker(result: PatentResult, idx: int) -> Tuple[Any, ...]:
    return (
        -(float(result.score) if result.score is not None else 0.0),
        -max(0, result.citation_count),
        result.publication_number,
        result.title,
        idx,
    )


def build_patent_config(
    threshold: float = 0.3,
    max_chars: int = 520,
    profile: str = "quality",
    threshold_mode: str = "prefer",
) -> DomainConfig[PatentResult]:
    effective_profile = parse_ranking_profile(profile)
    effective_threshold_mode = parse_threshold_mode(threshold_mode)

    def weights(query: str, ctx: RerankContext) -> Dict[str, float]:
        if effective_profile == "semantic":
            return {
                TEXT: 1.0, SOURCE_SCORE: 0.0, FRESHNESS: 0.0,
                CITATIONS: 0.0, STATUS: 0.0,
            }
        if effective_profile == "fast":
            return {
                TEXT: 0.0, SOURCE_SCORE: 1.0, FRESHNESS: 0.0,
                CITATIONS: 0.0, STATUS: 0.0,
            }
        return _patent_weights(query, ctx)

    def tiebreaker(result: PatentResult, idx: int) -> Tuple[Any, ...]:
        if effective_profile == "semantic":
            return (_patent_key(result, idx), idx)
        if effective_profile == "fast":
            return (idx,)
        return _patent_tiebreaker(result, idx)

    return DomainConfig(
        name="patent",
        key_fn=_patent_key,
        compress_fn=lambda p: _compress_patent_text(p, max_chars=max_chars),
        feature_fns={
            SOURCE_SCORE: _patent_source_score_feature,
            FRESHNESS: _patent_freshness_feature,
            CITATIONS: _patent_citations_feature,
            STATUS: _patent_status_feature,
        },
        weight_fn=weights,
        profile=effective_profile,
        threshold=threshold,
        threshold_mode=effective_threshold_mode,
        score_text=effective_profile != "fast",
        tiebreaker_fn=tiebreaker,
    )


class FusionReranker(Reranker):
    """辅助信号融合:在文本相关性基础上融合新鲜度、来源权威度、源内排名。

    final_score = α·text + β·freshness + γ·authority + δ·rank_signal

    默认权重: α=0.7, β=0.15, γ=0.1, δ=0.05
    """

    def __init__(
        self,
        inner: Reranker,
        time_sensitive: bool = False,
        alpha: float = 0.7,
        beta: float = 0.15,
        gamma: float = 0.10,
        delta: float = 0.05,
        authority_weights: Optional[dict] = None,
    ):
        self._inner = inner
        self._time_sensitive = time_sensitive
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._delta = delta
        self._authority = authority_weights or _DEFAULT_AUTHORITY
        self.name = inner.name
        self.supports_text_scoring = inner.supports_text_scoring

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        """领域融合只作用于结果排序；文本打分能力由内部 scorer 提供。"""
        return self._inner.score(query, texts)

    def _freshness_score(self, days_ago: Optional[int]) -> float:
        """新鲜度分数(0-1)。时效查询用指数衰减,非时效查询用线性衰减。"""
        if days_ago is None:
            return 0.5  # 无日期信息,给中性分
        if self._time_sensitive:
            return 1.0 / (1.0 + days_ago)  # 时效:1天=0.5, 7天=0.125
        return max(0.0, 1.0 - days_ago / 365.0)  # 非时效:线性衰减到0(1年)

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        ranked = self._inner.rerank(query, results, top_k)
        if not ranked:
            return ranked

        for r in ranked:
            text = r.rerank_score or 0.0
            freshness = self._freshness_score(_parse_date_days_ago(r.date))
            authority = self._authority.get(r.source, 0.8)
            rank_signal = 1.0 / (1.0 + (r.provider_rank or 0))

            r.rerank_score = (
                self._alpha * text
                + self._beta * freshness
                + self._gamma * authority
                + self._delta * rank_signal
            )

        return sorted(ranked, key=lambda r: r.rerank_score or 0.0, reverse=True)[:top_k]


class AcademicReranker(Reranker):
    """学术专用重排:先做文本相关性,再融合论文元数据。

    目标不是覆盖文本打分,而是利用论文的结构化信号做稳健精排:
    - citations: 更偏向经典/综述/基础论文
    - freshness: 在 "latest/recent/最新" 查询下提高新论文
    - venue: 轻量区分正式 venue 与预印本
    - OA: 小权重奖励可直接获取全文的结果
    """

    def __init__(
        self,
        inner: Reranker,
        max_docs: int = 25,
        max_chars: int = 480,
        threshold: float = 0.3,
        profile: str = "quality",
        threshold_mode: str = "prefer",
    ):
        self._inner = inner
        self._max_docs = max_docs
        self._max_chars = max_chars
        self._config = build_academic_config(
            threshold=threshold,
            max_docs=max_docs,
            max_chars=max_chars,
            profile=profile,
            threshold_mode=threshold_mode,
        )
        self.name = f"academic:{inner.name}"
        self.supports_text_scoring = inner.supports_text_scoring

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return self._inner.score(query, texts)

    def rerank_with_context(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int,
        ctx: RerankContext,
    ) -> List[SearchResult]:
        if not results:
            return []
        if not all(isinstance(r, AcademicResult) for r in results):
            return self._inner.rerank(query, results, top_k)
        papers = [r for r in results if isinstance(r, AcademicResult)]
        return rerank_domain(query, papers, self._config, self._inner, ctx, top_k)

    def _compress(self, result: AcademicResult) -> AcademicResult:
        """压缩论文文本,尽量让每篇论文只占一个 passage 预算。"""
        body = (result.content or result.snippet or "").strip()
        available = max(0, self._max_chars - len(result.title) - 1)
        trimmed = body[:available]
        compact = result.model_copy(deep=True)
        compact.content = trimmed
        compact.snippet = trimmed[:300]
        return compact

    @staticmethod
    def _normalize_list(values: List[Optional[float]], default: float = 0.0) -> List[float]:
        nums = [float(v) for v in values if v is not None]
        if not nums:
            return [default for _ in values]
        lo, hi = min(nums), max(nums)
        if math.isclose(lo, hi):
            flat = 0.5 if hi > 0 else default
            return [flat if v is not None else default for v in values]
        return [
            ((float(v) - lo) / (hi - lo)) if v is not None else default
            for v in values
        ]

    @staticmethod
    def _rank_fallback(n: int) -> List[float]:
        if n <= 0:
            return []
        vals = [1.0 / (1.0 + i) for i in range(n)]
        lo, hi = vals[-1], vals[0]
        if math.isclose(lo, hi):
            return [1.0 for _ in vals]
        return [(v - lo) / (hi - lo) for v in vals]

    def _base_scores(self, results: List[AcademicResult]) -> List[float]:
        rerank_scores = [r.rerank_score for r in results]
        if all(s is not None for s in rerank_scores):
            return [max(0.0, min(1.0, float(s))) for s in rerank_scores]  # type: ignore[arg-type]

        score_norm = self._normalize_list([r.score for r in results], default=0.0)
        rank_norm = self._rank_fallback(len(results))
        if any(r.score is not None for r in results):
            return [0.8 * s + 0.2 * rk for s, rk in zip(score_norm, rank_norm)]
        return rank_norm

    @staticmethod
    def _citation_scores(results: List[AcademicResult]) -> List[float]:
        return AcademicReranker._normalize_list(
            [math.log1p(max(0, r.citations)) for r in results], default=0.0
        )

    @staticmethod
    def _venue_score(venue: str) -> float:
        if not venue:
            return 0.0
        lower = venue.lower()
        if "arxiv" in lower or "biorxiv" in lower or "medrxiv" in lower or "ssrn" in lower:
            return 0.35
        return 1.0

    def _freshness_score(self, result: AcademicResult, wants_recent: bool) -> float:
        days_ago = _parse_date_days_ago(result.date)
        if days_ago is None and result.year:
            try:
                dt = datetime(int(result.year), 1, 1, tzinfo=timezone.utc)
                days_ago = max(0, (datetime.now(timezone.utc) - dt).days)
            except (TypeError, ValueError):
                days_ago = None
        if days_ago is None:
            return 0.4 if wants_recent else 0.5
        if wants_recent:
            # 学术里 "最新" 的时间尺度通常按年看,衰减比 web 更缓。
            return 1.0 / (1.0 + days_ago / 365.0)
        return max(0.0, 1.0 - days_ago / (365.0 * 20.0))

    @staticmethod
    def _oa_score(result: AcademicResult) -> float:
        if result.oa_pdf_url:
            return 1.0
        if result.oa_landing_url or result.is_oa:
            return 0.7
        return 0.0

    @staticmethod
    def _weights(query: str) -> Tuple[float, float, float, float, float]:
        q = (query or "").lower()
        wants_recent = bool(_RECENT_QUERY_RE.search(q))
        wants_foundational = bool(_FOUNDATIONAL_QUERY_RE.search(q))
        if wants_recent and wants_foundational:
            return 0.66, 0.16, 0.12, 0.04, 0.02
        if wants_recent:
            return 0.68, 0.08, 0.18, 0.04, 0.02
        if wants_foundational:
            return 0.66, 0.26, 0.01, 0.05, 0.02
        return 0.70, 0.20, 0.02, 0.05, 0.03

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        return self.rerank_with_context(
            query, results, top_k, build_rerank_context(query)
        )


class WebReranker(Reranker):
    """Web 专用重排:RRF 融合去重 + 文档级 SiliconFlow 语义精排。

    设计目标:
    1. 先用 RRF 融合多源结果,把“不同比分体系”统一成稳定候选序
    2. 每篇网页压成一个短 passage,避免长正文拆太多 chunk 稀释覆盖
    3. 用文本相关性做主排序,再混入少量 RRF prior 作为稳定先验
    4. 不依赖 threshold 过滤后的返回条数,而是对全候选都计算最终分
    """

    def __init__(
        self,
        inner: Reranker,
        max_chars: int = 320,
        text_weight: float = 0.85,
        rrf_weight: float = 0.15,
        pass_bonus: float = 0.02,
        threshold: float = 0.3,
        profile: str = "quality",
        threshold_mode: str = "prefer",
    ):
        self._inner = inner
        self._max_chars = max_chars
        self._text_weight = text_weight
        self._rrf_weight = rrf_weight
        self._pass_bonus = pass_bonus
        self._config = build_web_config(
            threshold=threshold,
            max_chars=max_chars,
            text_weight=text_weight,
            rrf_weight=rrf_weight,
            pass_bonus=pass_bonus,
            profile=profile,
            threshold_mode=threshold_mode,
        )
        self.name = f"web:{inner.name}"
        self.supports_text_scoring = inner.supports_text_scoring

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return self._inner.score(query, texts)

    @staticmethod
    def _stable_fused(results: List[SearchResult]) -> List[SearchResult]:
        fused = rrf_fuse(results, top_k=None)
        return sorted(
            fused,
            key=lambda r: (
                -(r.rerank_score or 0.0),
                r.provider_rank if r.provider_rank is not None else 10 ** 9,
                r.url,
            ),
        )

    @staticmethod
    def _normalize(values: List[Optional[float]], default: float = 0.0) -> List[float]:
        nums = [float(v) for v in values if v is not None]
        if not nums:
            return [default for _ in values]
        lo, hi = min(nums), max(nums)
        if math.isclose(lo, hi):
            flat = 0.5 if hi > 0 else default
            return [flat if v is not None else default for v in values]
        return [
            ((float(v) - lo) / (hi - lo)) if v is not None else default
            for v in values
        ]

    def _compress(self, result: SearchResult) -> SearchResult:
        parts: List[str] = []
        title = (result.title or "").strip()
        snippet = (result.snippet or "").strip()
        content = (result.content or "").strip()
        if title:
            parts.append(title)
        if snippet:
            parts.append(snippet)
        if content and content != snippet:
            parts.append(content)
        body = "\n".join(parts).strip()
        compact = result.model_copy(deep=True)
        compact.content = body[: self._max_chars]
        compact.snippet = compact.content[:200]
        return compact

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        return self.rerank_with_context(
            query, results, top_k, build_rerank_context(query)
        )

    def rerank_with_context(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int,
        ctx: RerankContext,
    ) -> List[SearchResult]:
        return rerank_domain(query, results, self._config, self._inner, ctx, top_k)


class PatentReranker(Reranker):
    """专利专用重排:文本相关性 + ES 先验 + 日期/引用/状态等专利信号。"""

    def __init__(
        self,
        inner: Reranker,
        max_chars: int = 520,
        threshold: float = 0.3,
        profile: str = "quality",
        threshold_mode: str = "prefer",
    ):
        self._inner = inner
        self._config = build_patent_config(
            threshold=threshold,
            max_chars=max_chars,
            profile=profile,
            threshold_mode=threshold_mode,
        )
        self.name = f"patent:{inner.name}"
        self.supports_text_scoring = inner.supports_text_scoring

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        return self._inner.score(query, texts)

    def rerank_with_context(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int,
        ctx: RerankContext,
    ) -> List[SearchResult]:
        if not results:
            return []
        if not all(isinstance(r, PatentResult) for r in results):
            return self._inner.rerank(query, results, top_k)
        patents = [r for r in results if isinstance(r, PatentResult)]
        return rerank_domain(query, patents, self._config, self._inner, ctx, top_k)

    def rerank(self, query: str, results: List[SearchResult], top_k: int) -> List[SearchResult]:
        return self.rerank_with_context(
            query, results, top_k, build_rerank_context(query)
        )


def build_reranker(
    enabled: bool,
    backend: str,
    model_name: str,
    cache_dir: str,
    device: Optional[str] = None,
    chunk_max_chars: int = 400,
    chunk_overlap: int = 50,
    threshold: float = 0.3,
    siliconflow_api_key: str = "",
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1",
    fusion_enabled: bool = True,
    fusion_time_sensitive: bool = False,
    fusion_alpha: float = 0.7,
    fusion_beta: float = 0.15,
    fusion_gamma: float = 0.10,
    fusion_delta: float = 0.05,
) -> Reranker:
    """构建重排器:backend → ThresholdReranker → FusionReranker,失败回退 NoOp。"""
    inner = build_text_scorer(
        enabled=enabled,
        backend=backend,
        model_name=model_name,
        cache_dir=cache_dir,
        device=device,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
        siliconflow_api_key=siliconflow_api_key,
        siliconflow_base_url=siliconflow_base_url,
    )
    if isinstance(inner, NoOpReranker):
        return inner
    r: Reranker = ThresholdReranker(inner, threshold=threshold)
    if fusion_enabled:
        r = FusionReranker(
            r,
            time_sensitive=fusion_time_sensitive,
            alpha=fusion_alpha, beta=fusion_beta,
            gamma=fusion_gamma, delta=fusion_delta,
        )
    return r


def build_text_scorer(
    enabled: bool,
    backend: str,
    model_name: str,
    cache_dir: str,
    device: Optional[str] = None,
    chunk_max_chars: int = 400,
    chunk_overlap: int = 50,
    siliconflow_api_key: str = "",
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1",
) -> Reranker:
    """构建纯文本 scorer。backend 只返回文本相关性分,不做领域融合。"""
    if not enabled or backend == "none":
        return NoOpReranker()
    try:
        if backend == "bge":
            return BGEReranker(
                model_name, device=device,
                chunk_max_chars=chunk_max_chars, chunk_overlap=chunk_overlap,
            )
        elif backend == "flashrank":
            return FlashRankReranker(
                model_name, cache_dir,
                chunk_max_chars=chunk_max_chars, chunk_overlap=chunk_overlap,
            )
        elif backend == "siliconflow":
            if not siliconflow_api_key:
                print("[rerank] siliconflow: 未配置 SILICONFLOW_API_KEY,回退 NoOp")
                return NoOpReranker()
            return SiliconFlowReranker(
                api_key=siliconflow_api_key, base_url=siliconflow_base_url,
                model=model_name,
                chunk_max_chars=chunk_max_chars, chunk_overlap=chunk_overlap,
            )
        else:
            return NoOpReranker()
    except Exception as e:
        print(f"[rerank] text_scorer backend={backend} 不可用,回退 NoOp: {e}")
        return NoOpReranker()
