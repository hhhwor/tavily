"""不可变的召回、排序与富化阶段文档。"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from src.models import AcademicResult, PatentResult, SearchResult

DocumentKind = Literal["web", "academic", "patent"]
ContentKind = Literal[
    "web_content",
    "web_snippet",
    "abstract",
    "patent_abstract",
    "title",
]


def _freeze(value: Any) -> Any:
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(tuple((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, FrozenMap):
        return {key: _thaw(item) for key, item in value._items}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FrozenMap(Mapping[str, Any]):
    """递归冻结的只读映射，可安全跨缓存和阶段共享。"""

    _items: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]] = None) -> "FrozenMap":
        frozen = _freeze(value or {})
        return frozen if isinstance(frozen, cls) else cls()

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self)


@dataclass(frozen=True, slots=True)
class SourceAttribution:
    provider: str
    provider_rank: Optional[int] = None
    source_record_id: Optional[str] = None
    snapshot: Optional[str] = None
    actual_filters: FrozenMap = field(default_factory=FrozenMap)


_BASE_FIELDS = {
    "url",
    "title",
    "snippet",
    "content",
    "date",
    "site",
    "score",
    "rerank_score",
    "provider_rank",
    "source",
    "raw",
}
_PDF_FIELDS = (
    "pdf_status",
    "pdf_text",
    "pdf_pages",
    "pdf_text_length",
    "pdf_returned_chars",
    "pdf_chunk_index",
    "pdf_page_from",
    "pdf_page_to",
    "pdf_next_cursor",
    "pdf_error_code",
    "pdf_error_message",
)


def _model_class(kind: DocumentKind):
    if kind == "academic":
        return AcademicResult
    if kind == "patent":
        return PatentResult
    return SearchResult


def _source_record_id(result: SearchResult, kind: DocumentKind) -> str:
    if kind == "academic" and isinstance(result, AcademicResult):
        return result.work_id or result.doi or result.url
    if kind == "patent" and isinstance(result, PatentResult):
        return result.publication_number or result.application_number or result.url
    raw_id = (result.raw or {}).get("id") or (result.raw or {}).get("result_id")
    return str(raw_id or result.url)


def _content_kind(result: SearchResult, kind: DocumentKind) -> ContentKind:
    if kind == "academic":
        return "abstract"
    if kind == "patent":
        return "patent_abstract"
    if result.source == "serpapi" or (not result.content and result.snippet):
        return "web_snippet"
    if result.content:
        return "web_content"
    return "web_snippet"


@dataclass(frozen=True, slots=True)
class RetrievedDocument:
    """Provider 结果的不可变规范形式，不携带排序工作状态。"""

    kind: DocumentKind
    url: str
    title: str
    snippet: str
    content: str
    published_date: str
    site: str
    source: str
    source_score: Optional[float]
    content_kind: ContentKind
    attributions: tuple[SourceAttribution, ...]
    metadata: FrozenMap = field(default_factory=FrozenMap, repr=False)
    raw_payload: FrozenMap = field(default_factory=FrozenMap, repr=False)

    @classmethod
    def from_result(
        cls,
        result: SearchResult,
        kind: DocumentKind,
        *,
        provider_rank: Optional[int] = None,
        attributions: Optional[tuple[SourceAttribution, ...]] = None,
        snapshot: Optional[str] = None,
        actual_filters: Optional[Mapping[str, Any]] = None,
        content_kind: Optional[ContentKind] = None,
    ) -> "RetrievedDocument":
        payload = result.model_dump(mode="python")
        raw = dict(payload.pop("raw", {}) or {})
        # RRF 工作字段只能进入 RankedDocument.features，不能污染来源载荷。
        raw = {key: value for key, value in raw.items() if not key.startswith("_rrf_")}
        metadata = {key: value for key, value in payload.items() if key not in _BASE_FIELDS}
        attribution = SourceAttribution(
            provider=result.source,
            provider_rank=(
                result.provider_rank if provider_rank is None else provider_rank
            ),
            source_record_id=_source_record_id(result, kind),
            snapshot=snapshot,
            actual_filters=FrozenMap.from_mapping(actual_filters),
        )
        return cls(
            kind=kind,
            url=result.url,
            title=result.title,
            snippet=result.snippet,
            content=result.content,
            published_date=result.date,
            site=result.site,
            source=result.source,
            source_score=result.score,
            content_kind=content_kind or _content_kind(result, kind),
            attributions=attributions or (attribution,),
            metadata=FrozenMap.from_mapping(metadata),
            raw_payload=FrozenMap.from_mapping(raw),
        )

    @property
    def primary_provider_rank(self) -> Optional[int]:
        return self.attributions[0].provider_rank if self.attributions else None

    def result_data(self, *, rerank_score: Optional[float] = None) -> dict[str, Any]:
        data = self.metadata.to_dict()
        data.update({
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "content": self.content,
            "date": self.published_date,
            "site": self.site,
            "score": self.source_score,
            "rerank_score": rerank_score,
            "provider_rank": self.primary_provider_rank,
            "source": self.source,
            "raw": self.raw_payload.to_dict(),
        })
        return data

    def to_result(self) -> SearchResult:
        return _model_class(self.kind).model_validate(self.result_data())


@dataclass(frozen=True, slots=True)
class RankedDocument:
    """排序阶段输出；分数和特征与来源文档分离。"""

    document: RetrievedDocument
    score: Optional[float]
    ranking_profile: str
    features: FrozenMap = field(default_factory=FrozenMap)

    @classmethod
    def from_result(
        cls,
        result: SearchResult,
        kind: DocumentKind,
        *,
        ranking_profile: str,
        attributions: tuple[SourceAttribution, ...],
        content_kind: Optional[ContentKind] = None,
    ) -> "RankedDocument":
        raw = result.raw or {}
        features = {
            key.removeprefix("_rrf_"): value
            for key, value in raw.items()
            if key.startswith("_rrf_")
        }
        document = RetrievedDocument.from_result(
            result,
            kind,
            attributions=attributions,
            content_kind=content_kind,
        )
        return cls(
            document=document,
            score=result.rerank_score,
            ranking_profile=ranking_profile,
            features=FrozenMap.from_mapping(features),
        )

    @property
    def kind(self) -> DocumentKind:
        return self.document.kind

    @property
    def attributions(self) -> tuple[SourceAttribution, ...]:
        return self.document.attributions

    def to_result(self) -> SearchResult:
        return _model_class(self.kind).model_validate(
            self.document.result_data(rerank_score=self.score)
        )


@dataclass(frozen=True, slots=True)
class EnrichedDocument:
    """富化阶段输出；PDF 数据是独立只读增量。"""

    ranked: RankedDocument
    enrichment: FrozenMap = field(default_factory=FrozenMap)

    @classmethod
    def from_result(
        cls,
        ranked: RankedDocument,
        result: AcademicResult,
    ) -> "EnrichedDocument":
        return cls(
            ranked=ranked,
            enrichment=FrozenMap.from_mapping({
                name: getattr(result, name) for name in _PDF_FIELDS
            }),
        )

    @property
    def kind(self) -> DocumentKind:
        return self.ranked.kind

    @property
    def score(self) -> Optional[float]:
        return self.ranked.score

    def to_result(self) -> SearchResult:
        data = self.ranked.document.result_data(rerank_score=self.ranked.score)
        data.update(self.enrichment.to_dict())
        return _model_class(self.kind).model_validate(data)
