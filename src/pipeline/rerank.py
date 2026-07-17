"""Deprecated compatibility exports; use :mod:`src.ranking` for new code."""

from src.ranking.academic import AcademicReranker, build_academic_config
from src.ranking.adapters import BGEReranker, FlashRankReranker, SiliconFlowReranker
from src.ranking.core import (
    DomainConfig,
    RerankContext,
    build_rerank_context,
    rerank_domain,
)
from src.ranking.factory import build_text_scorer
from src.ranking.legacy import FusionReranker, ThresholdReranker, build_reranker
from src.ranking.patent import PatentReranker, build_patent_config
from src.ranking.ports import (
    NoOpReranker,
    Reranker,
    clamp01,
    sigmoid_normalize,
)
from src.ranking.web import WebReranker, build_web_config

__all__ = [
    "AcademicReranker",
    "BGEReranker",
    "DomainConfig",
    "FlashRankReranker",
    "FusionReranker",
    "NoOpReranker",
    "PatentReranker",
    "RerankContext",
    "Reranker",
    "SiliconFlowReranker",
    "ThresholdReranker",
    "WebReranker",
    "build_academic_config",
    "build_patent_config",
    "build_rerank_context",
    "build_reranker",
    "build_text_scorer",
    "build_web_config",
    "clamp01",
    "rerank_domain",
    "sigmoid_normalize",
]
