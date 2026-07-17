"""搜索引擎编排:多源并发检索 → 去重 → 重排 → Evidence。

web 搜索(腾讯/百度/SerpAPI)、学术检索(OpenAlex)与专利检索并发召回、
独立重排,最终统一为按相关性混排的 evidence[] 返回给 Agent。
"""
from __future__ import annotations

import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Optional, Sequence
from urllib.parse import quote

import requests

from src.config import settings
from src.cache import build_cache
from src.l0 import plan_query, rewrite_academic_query
from src.models import (
    AcademicResult,
    Answerability,
    AnswerabilityGap,
    CandidateClaim,
    Evidence,
    EvidenceAccess,
    EvidenceCitation,
    EvidenceDiagnostics,
    EvidencePatent,
    EvidencePassage,
    EvidenceScores,
    PatentResult,
    PdfTextResponse,
    SearchFailure,
    SearchBoundary,
    SearchPlan,
    SearchResponse,
    SearchResult,
    VerifyResponse,
)
from src.pipeline.rerank import (
    AcademicReranker,
    PatentReranker,
    WebReranker,
    build_rerank_context,
    build_text_scorer,
)
from src.pipeline.ranking_options import resolve_ranking_options
from src.providers.base import SearchProvider
from src.trust import annotate_evidence, build_claim_verifier, build_search_boundary

_EVIDENCE_PASSAGE_MAX_CHARS = 1800


def _short_hash(*values: object) -> str:
    raw = "|".join(str(v or "") for v in values)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _clip_evidence_text(text: str) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= _EVIDENCE_PASSAGE_MAX_CHARS:
        return text, False
    return text[:_EVIDENCE_PASSAGE_MAX_CHARS].rstrip() + "…", True


def _evidence_relevance(result: SearchResult, rank: int) -> float:
    if result.rerank_score is not None:
        return float(result.rerank_score)
    return 1.0 / max(1, rank + 1)


def _citation_label(authors: List[str], year: Optional[int], title: str) -> str:
    if authors:
        first = authors[0].split(",")[0].strip() or authors[0].strip()
        suffix = " et al." if len(authors) > 1 else ""
        return f"{first}{suffix}, {year}" if year else f"{first}{suffix}"
    return f"{title[:48]}, {year}" if year else title[:64]


def _search_failure(
    *,
    stage: str,
    source: str,
    source_type: Optional[str],
    code: str,
    message: object,
    recoverable: bool = True,
) -> SearchFailure:
    return SearchFailure(
        stage=stage,
        source=source,
        type=source_type,
        code=code,
        message=str(message)[:500],
        recoverable=recoverable,
    )


def _evidence_type_counts(evidence: List[Evidence]) -> dict[str, int]:
    counts = {"web": 0, "academic": 0, "patent": 0}
    for item in evidence:
        if item.type in counts:
            counts[item.type] += 1
    return counts


def _build_answerability(
    evidence: List[Evidence],
    failures: List[SearchFailure],
    *,
    expected_web: bool,
    expected_academic: bool,
    expected_patent: bool,
    include_pdf_text: bool,
) -> Answerability:
    counts = _evidence_type_counts(evidence)
    gaps: List[AnswerabilityGap] = []

    if failures:
        gaps.append(AnswerabilityGap(
            code="PARTIAL_FAILURE",
            severity="warning",
            message=f"{len(failures)} 个检索子任务失败; 详见 failures[]。",
        ))

    expected = [
        ("web", expected_web, "NO_WEB_EVIDENCE", "未返回网页证据。"),
        ("academic", expected_academic, "NO_ACADEMIC_EVIDENCE", "查询需要学术证据,但未返回论文证据。"),
        ("patent", expected_patent, "NO_PATENT_EVIDENCE", "查询需要专利证据,但未返回专利证据。"),
    ]
    for source_type, needed, code, message in expected:
        if needed and counts[source_type] == 0:
            gaps.append(AnswerabilityGap(
                code=code,
                severity="warning",
                message=message,
                type=source_type,
            ))

    if not evidence:
        gaps.insert(0, AnswerabilityGap(
            code="NO_EVIDENCE",
            severity="blocking",
            message="没有可用证据,不应直接回答。",
        ))
    elif len(evidence) < 3:
        gaps.append(AnswerabilityGap(
            code="LOW_EVIDENCE_COUNT",
            severity="info",
            message=f"仅返回 {len(evidence)} 条证据,回答时应降低确定性。",
        ))

    if include_pdf_text:
        pdf_gap_count = sum(
            1 for item in evidence
            if item.type == "academic"
            and item.access.oa_pdf_url
            and item.passage.snippet_type != "pdf_text"
        )
        if pdf_gap_count:
            gaps.append(AnswerabilityGap(
                code="PDF_TEXT_UNAVAILABLE",
                severity="warning",
                message=f"{pdf_gap_count} 条论文证据只有摘要或元数据,未拿到 PDF 正文。",
                type="academic",
            ))

    partial_count = sum(1 for item in evidence if item.diagnostics.partial)
    if partial_count:
        gaps.append(AnswerabilityGap(
            code="PARTIAL_EVIDENCE",
            severity="info",
            message=f"{partial_count} 条证据被截断或仍有后续内容。",
        ))

    if not evidence:
        return Answerability(status="not_answerable", confidence="none", gaps=gaps)
    if any(gap.severity in {"blocking", "warning"} for gap in gaps):
        missing_required = any(
            gap.code in {"NO_WEB_EVIDENCE", "NO_ACADEMIC_EVIDENCE", "NO_PATENT_EVIDENCE"}
            for gap in gaps
        )
        confidence = "low" if len(evidence) < 3 or missing_required else "medium"
        return Answerability(status="partial", confidence=confidence, gaps=gaps)
    confidence = "high" if len(evidence) >= 3 else "medium"
    return Answerability(status="answerable", confidence=confidence, gaps=gaps)


def _build_providers() -> List[SearchProvider]:
    providers: List[SearchProvider] = []
    for name in settings.enabled_providers:
        try:
            if name == "tencent":
                from src.providers.tencent import TencentSearchProvider

                providers.append(TencentSearchProvider(timeout=settings.provider_timeout))
            elif name == "baidu":
                from src.providers.baidu import BaiduSearchProvider

                providers.append(BaiduSearchProvider(timeout=settings.provider_timeout))
            elif name == "serpapi":
                from src.providers.serpapi import SerpApiProvider

                providers.append(SerpApiProvider(timeout=settings.provider_timeout))
        except Exception as e:  # 凭证缺失等
            print(f"[engine] 跳过 provider {name}: {e}")
    return providers


def _build_academic_provider() -> Optional[SearchProvider]:
    """学术检索源(OpenAlex);未启用或构建失败返回 None。"""
    if not settings.academic_enabled:
        return None
    try:
        from src.providers.openalex import OpenAlexProvider

        return OpenAlexProvider(
            base_url=settings.openalex_api_url,
            api_key=settings.openalex_api_key,
            per_page=settings.openalex_per_page,
            timeout=settings.provider_timeout,
        )
    except Exception as e:
        print(f"[engine] 跳过学术源 openalex_local: {e}")
        return None


def _build_patent_provider() -> Optional[SearchProvider]:
    """专利检索源(houdutech 只读 ES);未启用或构建失败返回 None。"""
    if not settings.patent_enabled:
        return None
    try:
        from src.providers.patent_es import PatentEsProvider

        return PatentEsProvider(
            base_url=settings.patent_es_url,
            index=settings.patent_es_index,
            timeout=settings.provider_timeout,
            verify_tls=settings.patent_es_verify_tls,
            per_page=settings.patent_es_per_page,
        )
    except Exception as e:
        print(f"[engine] 跳过专利源 patent_es: {e}")
        return None


class SearchEngine:
    def __init__(self) -> None:
        self.providers = _build_providers()
        self.academic_provider = _build_academic_provider()
        self.patent_provider = _build_patent_provider()
        self._text_scorer_cache: dict = {}  # 按请求参数缓存 scorer(避免本地模型重复加载)
        self.cache = build_cache(settings.cache_backend, settings.cache_max_size) \
            if settings.cache_enabled else None  # provider 召回结果缓存
        self.text_scorer = self._make_text_scorer(
            settings.rerank_enabled, settings.rerank_backend,
            settings.rerank_model,
        )
        try:
            self.claim_verifier = self._make_claim_verifier()
        except Exception as exc:
            print(f"[engine] 陈述校验模型不可用,降级到规则: {exc}")
            self.claim_verifier = build_claim_verifier(
                backend="rules",
                api_key="",
                base_url=settings.siliconflow_base_url,
                model=settings.trust_verify_model,
                timeout=settings.trust_verify_timeout,
                max_claims=settings.trust_verify_max_claims,
                max_evidence_per_claim=settings.trust_verify_max_evidence,
            )
        if not self.providers and not self.academic_provider and not self.patent_provider:
            print("[engine] 警告:无可用搜索源,请检查 .env 凭证")

    def _cached_search(
        self, prov: SearchProvider, query: str, k: int,
        recency: Optional[str], use_cache: bool,
    ) -> List[SearchResult]:
        """带缓存的 provider 召回。命中/写入均用深拷贝,避免缓存对象被后续重排原地修改污染。

        key 由 (provider, k, recency, query) 构成 —— provider 自身配置(如 openalex
        topic_filter)进程内不变,故不入 key。
        """
        if not use_cache or self.cache is None:
            return prov.search(query, k, recency)
        ck = f"{prov.name}|{k}|{recency or ''}|{query}"
        hit = self.cache.get(ck)
        if hit is not None:
            return [r.model_copy(deep=True) for r in hit]  # 返回副本,后续修改不污染缓存
        items = prov.search(query, k, recency)
        self.cache.set(ck, [r.model_copy(deep=True) for r in items], settings.cache_ttl)
        return items

    def _make_text_scorer(self, enabled: bool, backend: str, model: str):
        """按给定参数构建文本 scorer(其余参数取全局 settings)。"""
        return build_text_scorer(
            enabled, backend, model, settings.rerank_cache_dir, settings.rerank_device,
            chunk_max_chars=settings.chunk_max_chars,
            chunk_overlap=settings.chunk_overlap,
            siliconflow_api_key=settings.siliconflow_api_key,
            siliconflow_base_url=settings.siliconflow_base_url,
        )

    @staticmethod
    def _make_claim_verifier():
        return build_claim_verifier(
            backend=settings.trust_verify_backend,
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model=settings.trust_verify_model,
            timeout=settings.trust_verify_timeout,
            max_claims=settings.trust_verify_max_claims,
            max_evidence_per_claim=settings.trust_verify_max_evidence,
        )

    def verify_claims(
        self,
        query: str,
        claims: Sequence[CandidateClaim],
        evidence: Sequence[Evidence],
        *,
        profile: str = "general",
        search_boundary: Optional[SearchBoundary] = None,
    ) -> VerifyResponse:
        """基于客户端传入的 Phase 0 evidence 做陈述级校验；不自动补充检索。"""
        verifier = getattr(self, "claim_verifier", None)
        if verifier is None:
            verifier = self._make_claim_verifier()
            self.claim_verifier = verifier
        return verifier.verify(
            query=query,
            claims=claims,
            evidence=evidence,
            profile=profile,
            search_boundary=search_boundary,
        )

    def _select_text_scorer(
        self,
        enabled: Optional[bool] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """选择文本 scorer:全部覆盖为 None 时复用默认单例(零开销);否则按覆盖参数
        构建并缓存。缓存上限 16,超出清空,防本地模型缓存无限增长。"""
        if enabled is None and backend is None and model is None:
            return self.text_scorer
        eff = (
            settings.rerank_enabled if enabled is None else enabled,
            backend or settings.rerank_backend,
            model or settings.rerank_model,
        )
        r = self._text_scorer_cache.get(eff)
        if r is None:
            if len(self._text_scorer_cache) >= 16:
                self._text_scorer_cache.clear()
            r = self._make_text_scorer(*eff)
            self._text_scorer_cache[eff] = r
        return r

    @staticmethod
    def _build_evidence(
        ranked: List[SearchResult],
        ranked_papers: List[AcademicResult],
        ranked_patents: List[PatentResult],
    ) -> List[Evidence]:
        evidence: List[Evidence] = []

        for rank, r in enumerate(ranked):
            text, clipped = _clip_evidence_text(r.content or r.snippet or r.title)
            if not text:
                continue
            result_key = _short_hash("web", r.source, r.url, r.title)
            warnings = ["TRUNCATED_EVIDENCE"] if clipped else []
            evidence.append(Evidence(
                id=f"web:{result_key}:content",
                result_id=f"web:{result_key}",
                type="web",
                source=r.source,
                title=r.title,
                url=r.url,
                published_date=r.date,
                passage=EvidencePassage(
                    text=text,
                    snippet_type="web_content" if r.content else "web_snippet",
                    char_start=0,
                    char_end=len(text),
                ),
                citation=EvidenceCitation(label=r.site or r.title[:64], venue=r.site),
                scores=EvidenceScores(
                    relevance=_evidence_relevance(r, rank),
                    source_rank=rank,
                    rerank_score=r.rerank_score,
                    confidence=_evidence_relevance(r, rank),
                ),
                access=EvidenceAccess(is_open=bool(r.url)),
                diagnostics=EvidenceDiagnostics(warnings=warnings, partial=clipped),
            ))

        for rank, p in enumerate(ranked_papers):
            result_id = f"academic:{p.work_id}" if p.work_id else f"academic:{_short_hash(p.doi, p.url, p.title)}"
            source_text = p.pdf_text or p.content or p.snippet or p.title
            text, clipped = _clip_evidence_text(source_text)
            if not text:
                continue
            snippet_type = "pdf_text" if p.pdf_text else "abstract"
            chunk_index = p.pdf_chunk_index if p.pdf_chunk_index is not None else 0
            evidence_id = f"{result_id}:pdf:{chunk_index}" if p.pdf_text else f"{result_id}:abstract"
            warnings: List[str] = []
            if clipped or p.pdf_next_cursor:
                warnings.append("TRUNCATED_EVIDENCE")
            if p.oa_pdf_url and not p.pdf_text and p.pdf_status in {"not_requested", "no_pdf_url"}:
                warnings.append("PDF_TEXT_UNAVAILABLE")
            if p.pdf_error_code:
                warnings.append(p.pdf_error_code)
            evidence.append(Evidence(
                id=evidence_id,
                result_id=result_id,
                type="academic",
                source=p.source,
                title=p.title,
                url=p.url or p.oa_landing_url or p.oa_pdf_url,
                published_date=p.date or (str(p.year) if p.year else ""),
                language=(p.raw or {}).get("language"),
                passage=EvidencePassage(
                    text=text,
                    snippet_type=snippet_type,
                    char_start=0,
                    char_end=len(text),
                    page_from=p.pdf_page_from if p.pdf_text else None,
                    page_to=p.pdf_page_to if p.pdf_text else None,
                    chunk_index=chunk_index if p.pdf_text else None,
                ),
                citation=EvidenceCitation(
                    label=_citation_label(p.authors, p.year, p.title),
                    authors=p.authors,
                    year=p.year,
                    venue=p.venue,
                    doi=p.doi or None,
                    work_id=p.work_id or None,
                ),
                scores=EvidenceScores(
                    relevance=_evidence_relevance(p, rank),
                    source_rank=rank,
                    rerank_score=p.rerank_score,
                    authority=float(p.citations) if p.citations else None,
                    confidence=_evidence_relevance(p, rank),
                ),
                access=EvidenceAccess(
                    is_open=p.is_oa,
                    license=p.license or None,
                    oa_pdf_url=p.oa_pdf_url or None,
                    pdf_status=p.pdf_status,
                    next_cursor=p.pdf_next_cursor,
                ),
                diagnostics=EvidenceDiagnostics(
                    warnings=warnings,
                    partial=bool(clipped or p.pdf_next_cursor),
                    failure_code=p.pdf_error_code,
                ),
            ))

        for rank, p in enumerate(ranked_patents):
            pub = p.publication_number or p.application_number or _short_hash(p.url, p.title)
            result_id = f"patent:{pub}"
            text, clipped = _clip_evidence_text(p.content or p.snippet or p.title)
            if not text:
                continue
            warnings = ["TRUNCATED_EVIDENCE"] if clipped else []
            evidence.append(Evidence(
                id=f"{result_id}:abstract",
                result_id=result_id,
                type="patent",
                source=p.source,
                title=p.title,
                url=p.url,
                published_date=p.publication_date or p.application_date,
                passage=EvidencePassage(
                    text=text,
                    snippet_type="patent_abstract",
                    char_start=0,
                    char_end=len(text),
                ),
                citation=EvidenceCitation(
                    label=pub,
                    publication_number=p.publication_number or None,
                ),
                patent=EvidencePatent(
                    publication_number=p.publication_number,
                    application_number=p.application_number,
                    applicant=p.applicant,
                    inventor=p.inventor,
                    ipc_main=p.ipc_main,
                    cpc_main=p.cpc_main,
                    country=p.country,
                    status=p.status,
                    family_id=p.family_id,
                    application_date=p.application_date,
                    publication_date=p.publication_date,
                    patent_type=p.patent_type,
                    citation_count=p.citation_count,
                ),
                scores=EvidenceScores(
                    relevance=_evidence_relevance(p, rank),
                    source_rank=rank,
                    rerank_score=p.rerank_score,
                    authority=float(p.citation_count) if p.citation_count else None,
                    confidence=_evidence_relevance(p, rank),
                ),
                access=EvidenceAccess(is_open=bool(p.url)),
                diagnostics=EvidenceDiagnostics(warnings=warnings, partial=clipped),
            ))

        return sorted(
            evidence,
            key=lambda item: (
                item.scores.relevance if item.scores.relevance is not None else 0.0,
                -(item.scores.source_rank or 0),
            ),
            reverse=True,
        )

    def _enrich_academic_pdf_text(
        self,
        papers: List[AcademicResult],
        *,
        include_pdf_text: bool,
        pdf_text_mode: Optional[str],
        pdf_max_results: Optional[int],
        pdf_max_chars_per_result: Optional[int],
        pdf_timeout_ms: Optional[int],
    ) -> None:
        """Optionally attach extracted PDF text to ranked academic results.

        This is intentionally a post-rerank enrichment step: it never affects
        recall/ranking, and per-paper failures are represented on the result.
        """
        if not include_pdf_text or not papers:
            return

        mode = (pdf_text_mode or settings.openalex_pdf_text_mode or "sync").strip().lower()
        if mode not in {"cached", "sync"}:
            mode = "sync"
        max_results = settings.openalex_pdf_max_results if pdf_max_results is None else pdf_max_results
        max_results = max(0, min(max_results, 5))
        max_chars = (
            settings.openalex_pdf_max_chars
            if pdf_max_chars_per_result is None
            else pdf_max_chars_per_result
        )
        max_chars = max(1, min(max_chars, 30000))
        timeout_ms = settings.openalex_pdf_timeout_ms if pdf_timeout_ms is None else pdf_timeout_ms
        timeout_ms = max(1000, min(timeout_ms, 60000))
        if max_results <= 0:
            return

        candidates: List[AcademicResult] = []
        for paper in papers:
            if not paper.work_id:
                paper.pdf_status = "failed"
                paper.pdf_error_code = "WORK_ID_MISSING"
                continue
            if not paper.oa_pdf_url:
                paper.pdf_status = "no_pdf_url"
                paper.pdf_error_code = "PDF_URL_MISSING"
                continue
            candidates.append(paper)
            if len(candidates) >= max_results:
                break
        if not candidates:
            return

        headers = {"Content-Type": "application/json"}
        if settings.openalex_api_key:
            headers["X-API-Key"] = settings.openalex_api_key
        endpoint = f"{settings.openalex_api_url.rstrip('/')}/openalex/pdf/extract"
        deadline = time.monotonic() + (settings.openalex_pdf_total_budget_ms / 1000)

        def _enrich_one(paper: AcademicResult) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                paper.pdf_status = "timeout"
                paper.pdf_error_code = "PDF_TOTAL_BUDGET_EXCEEDED"
                return
            budget_ms = min(timeout_ms, int(remaining * 1000))
            try:
                resp = requests.post(
                    endpoint,
                    json={
                        "work_id": paper.work_id,
                        "mode": mode,
                        "max_chars": max_chars,
                        "timeout_ms": budget_ms,
                    },
                    headers=headers,
                    timeout=max(1, budget_ms / 1000 + 2),
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.Timeout:
                paper.pdf_status = "timeout"
                paper.pdf_error_code = "DOWNLOAD_TIMEOUT"
                return
            except Exception as exc:
                paper.pdf_status = "failed"
                paper.pdf_error_code = "PDF_ENRICH_FAILED"
                paper.pdf_error_message = str(exc)[:300]
                return

            status = data.get("status") or "failed"
            paper.pdf_status = status
            paper.pdf_text = data.get("text") or ""
            paper.pdf_pages = data.get("pages")
            paper.pdf_text_length = int(data.get("text_length") or 0)
            paper.pdf_returned_chars = len(paper.pdf_text)
            paper.pdf_chunk_index = data.get("chunk_index")
            paper.pdf_page_from = data.get("page_from")
            paper.pdf_page_to = data.get("page_to")
            paper.pdf_next_cursor = data.get("next_cursor")
            paper.pdf_error_code = data.get("error_code")
            paper.pdf_error_message = data.get("error_message")

        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            futures = {pool.submit(_enrich_one, paper): paper for paper in candidates}
            for future in as_completed(futures):
                paper = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    paper.pdf_status = "failed"
                    paper.pdf_error_code = "PDF_ENRICH_WORKER_FAILED"
                    paper.pdf_error_message = str(exc)[:300]

    def get_pdf_text(
        self,
        work_id: str,
        cursor: Optional[str] = None,
        max_chars: Optional[int] = None,
    ) -> PdfTextResponse:
        """Read a cached OpenAlex PDF text page by cursor.

        This endpoint intentionally does not trigger extraction. Agents should
        call search with include_pdf_text=true first, then continue with the
        returned citation.work_id and access.next_cursor.
        """
        work_id = (work_id or "").strip()
        if not work_id:
            return PdfTextResponse(
                work_id="",
                status="failed",
                error_code="WORK_ID_MISSING",
                error_message="work_id is required",
            )
        chars = settings.openalex_pdf_max_chars if max_chars is None else max_chars
        chars = max(1, min(int(chars), 30000))
        endpoint = f"{settings.openalex_api_url.rstrip('/')}/openalex/pdf/text/{quote(work_id, safe='')}"
        params = {"max_chars": chars}
        if cursor:
            params["cursor"] = cursor
        headers = {}
        if settings.openalex_api_key:
            headers["X-API-Key"] = settings.openalex_api_key
        try:
            resp = requests.get(
                endpoint,
                params=params,
                headers=headers or None,
                timeout=max(1, settings.provider_timeout),
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.Timeout:
            return PdfTextResponse(
                work_id=work_id,
                status="failed",
                error_code="PDF_TEXT_TIMEOUT",
                error_message="PDF text read timed out",
            )
        except Exception as exc:
            return PdfTextResponse(
                work_id=work_id,
                status="failed",
                error_code="PDF_TEXT_READ_FAILED",
                error_message=str(exc)[:300],
            )

        text = data.get("text")
        return PdfTextResponse(
            work_id=data.get("work_id") or work_id,
            status=data.get("status") or "failed",
            chunk_index=data.get("chunk_index"),
            page_from=data.get("page_from"),
            page_to=data.get("page_to"),
            text=text,
            returned_chars=len(text or ""),
            next_cursor=data.get("next_cursor"),
            partial=bool(data.get("next_cursor")),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
        )

    def search(
        self, query: str, top_k: int = 0, include_academic: Optional[bool] = None,
        include_patent: Optional[bool] = None,
        *,
        rerank_enabled: Optional[bool] = None,
        rerank_backend: Optional[str] = None,
        rerank_model: Optional[str] = None,
        rerank_threshold: Optional[float] = None,
        fusion_enabled: Optional[bool] = None,
        ranking_profile: Optional[str] = None,
        rerank_threshold_mode: Optional[str] = None,
        rewrite_enabled: Optional[bool] = None,
        trust_mode: str = "annotate",
        include_pdf_text: bool = False,
        pdf_text_mode: Optional[str] = None,
        pdf_max_results: Optional[int] = None,
        pdf_max_chars_per_result: Optional[int] = None,
        pdf_timeout_ms: Optional[int] = None,
    ) -> SearchResponse:
        trust_mode = (trust_mode or "annotate").strip().lower()
        if trust_mode not in {"off", "annotate"}:
            raise ValueError("trust_mode 仅支持 off / annotate")
        top_k = top_k or settings.default_top_k
        t0 = time.time()
        query_time = datetime.now(timezone.utc)

        ranking = resolve_ranking_options(
            default_profile=settings.ranking_profile,
            default_threshold=settings.rerank_threshold,
            default_threshold_mode=settings.rerank_threshold_mode,
            ranking_profile=ranking_profile,
            rerank_enabled=rerank_enabled,
            fusion_enabled=fusion_enabled,
            rerank_backend=rerank_backend or settings.rerank_backend,
            rerank_threshold=rerank_threshold,
            rerank_threshold_mode=rerank_threshold_mode,
        )
        default_text_scoring = settings.ranking_profile != "fast"
        enabled_override = (
            None
            if ranking.text_scoring_enabled == default_text_scoring
            else ranking.text_scoring_enabled
        )
        text_scorer = self._select_text_scorer(
            enabled_override, rerank_backend, rerank_model
        )
        if not text_scorer.supports_text_scoring and ranking.threshold_mode != "off":
            ranking = ranking.disable_threshold("THRESHOLD_SKIPPED_NO_SCORER")
        reranker_options = {
            "profile": ranking.profile,
            "threshold": ranking.threshold,
            "threshold_mode": ranking.threshold_mode,
        }
        web_reranker = WebReranker(text_scorer, **reranker_options)
        academic_reranker = AcademicReranker(text_scorer, **reranker_options)
        patent_reranker = PatentReranker(text_scorer, **reranker_options)
        # 查询改写开关:请求未指定则用全局默认
        rewrite = settings.rewrite_enabled if rewrite_enabled is None else rewrite_enabled

        # 0) L0 查询理解:规范化 + 时效识别 + 学术意图识别 + (可选)LLM 改写
        plan = plan_query(
            query, [p.name for p in self.providers], top_k,
            rewrite=rewrite,
            rewrite_api_key=settings.siliconflow_api_key,
            rewrite_base_url=settings.siliconflow_base_url,
            rewrite_model=settings.rewrite_model,
            rewrite_cache_size=settings.rewrite_cache_size,
            academic_detect=settings.openalex_academic_detect,
            force_academic=include_academic,
            patent_detect=settings.patent_detect,
            force_patent=include_patent,
        )
        active = [p for p in self.providers if p.name in plan.providers]
        do_academic = self.academic_provider is not None and plan.academic
        do_patent = self.patent_provider is not None and plan.patent
        failures: List[SearchFailure] = list(plan.failures)
        if plan.academic and self.academic_provider is None:
            failures.append(_search_failure(
                stage="routing",
                source="openalex_local",
                source_type="academic",
                code="PROVIDER_UNAVAILABLE",
                message="学术检索被请求或自动触发,但 OpenAlex provider 未启用。",
            ))
        if plan.patent and self.patent_provider is None:
            failures.append(_search_failure(
                stage="routing",
                source="patent_es",
                source_type="patent",
                code="PROVIDER_UNAVAILABLE",
                message="专利检索被请求或自动触发,但 Patent ES provider 未启用。",
            ))
        # 用改写后的查询检索(若有),否则用规范化查询
        search_query = plan.rewritten_query or plan.normalized_query
        # 学术检索单独改写 query:把自然语言问句提取为论文标题/英文检索词(web 仍用原 query)
        academic_query = search_query
        if do_academic and settings.openalex_query_rewrite and settings.siliconflow_api_key:
            academic_query = rewrite_academic_query(
                search_query, settings.siliconflow_api_key,
                settings.siliconflow_base_url, settings.rewrite_model,
                settings.rewrite_cache_size,
                failures=failures,
            )
        ctx = build_rerank_context(search_query, time_sensitive=plan.time_sensitive)

        # 1) 并发召回:web 源 + (可选)学术源,同一个线程池
        #    缓存:provider 召回级;时效查询(time_sensitive)跳过缓存以保证新鲜度
        #    各 task 携带自己的 query(web 用原 query,学术用改写后 query)
        raw: List[SearchResult] = []
        papers: List[AcademicResult] = []
        patents: List[PatentResult] = []
        used: List[str] = []
        use_cache = settings.cache_enabled and self.cache is not None and not plan.time_sensitive
        # task: (kind, provider, query)
        tasks = [("web", p, search_query) for p in active]
        if do_academic:
            tasks.append(("academic", self.academic_provider, academic_query))
        if do_patent:
            # 专利用中文原 query(中文库;不走学术英文改写)
            tasks.append(("patent", self.patent_provider, search_query))

        if tasks:
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                futures = {
                    pool.submit(
                        self._cached_search, prov, q,
                        settings.per_provider_k, plan.recency, use_cache,
                    ): (kind, prov.name)
                    for kind, prov, q in tasks
                }
                for fut in as_completed(futures):
                    kind, name = futures[fut]
                    try:
                        items = fut.result()
                        if kind == "academic":
                            papers.extend(items)
                            if items:
                                used.append(name)  # 学术源也计入来源归属(供 providers_used)
                        elif kind == "patent":
                            patents.extend(items)
                            if items:
                                used.append(name)  # 专利源也计入来源归属
                        else:
                            for i, r in enumerate(items):
                                r.provider_rank = i  # 记录源内排名,供 RRF 融合
                            raw.extend(items)
                            used.append(name)
                    except Exception as e:
                        failures.append(_search_failure(
                            stage="provider_search",
                            source=name,
                            source_type=kind,
                            code="PROVIDER_SEARCH_FAILED",
                            message=e,
                        ))
                        print(f"[engine] provider {name} 失败: {e}")

        # 2) 多路独立重排(并发),复用线程安全的 text_scorer;请求上下文显式传入。
        def _rank_web() -> List[SearchResult]:
            return web_reranker.rerank_with_context(search_query, raw, top_k, ctx)

        def _rank_academic() -> List[AcademicResult]:
            if not papers:
                return []
            # 用改写后的学术检索词重排(英文↔英文论文打分更准,避免中文原query被阈值误杀)
            ranked = academic_reranker.rerank_with_context(academic_query, papers, top_k, ctx)
            return [r for r in ranked if isinstance(r, AcademicResult)]

        def _rank_patent() -> List[PatentResult]:
            if not patents:
                return []
            # 专利用中文原 query 重排;专利结构化信号由 PatentReranker 融合。
            ranked = patent_reranker.rerank_with_context(search_query, patents, top_k, ctx)
            return [r for r in ranked if isinstance(r, PatentResult)]

        ranked: List[SearchResult] = []
        ranked_papers: List[AcademicResult] = []
        ranked_patents: List[PatentResult] = []

        rank_jobs = [("web", _rank_web)]
        if papers:
            rank_jobs.append(("academic", _rank_academic))
        if patents:
            rank_jobs.append(("patent", _rank_patent))

        def _fallback_rank(kind: str):
            if kind == "academic":
                return papers[:top_k]
            if kind == "patent":
                return patents[:top_k]
            return raw[:top_k]

        def _assign_ranked(kind: str, items) -> None:
            nonlocal ranked, ranked_papers, ranked_patents
            if kind == "academic":
                ranked_papers = [r for r in items if isinstance(r, AcademicResult)]
            elif kind == "patent":
                ranked_patents = [r for r in items if isinstance(r, PatentResult)]
            else:
                ranked = [r for r in items if isinstance(r, SearchResult)]

        if len(rank_jobs) > 1:
            with ThreadPoolExecutor(max_workers=len(rank_jobs)) as pool:
                futures = {pool.submit(fn): kind for kind, fn in rank_jobs}
                for future in as_completed(futures):
                    kind = futures[future]
                    try:
                        _assign_ranked(kind, future.result())
                    except Exception as e:
                        failures.append(_search_failure(
                            stage="rerank",
                            source=f"{kind}_reranker",
                            source_type=kind,
                            code="RERANK_FAILED",
                            message=e,
                        ))
                        _assign_ranked(kind, _fallback_rank(kind))
        else:
            for kind, fn in rank_jobs:
                try:
                    _assign_ranked(kind, fn())
                except Exception as e:
                    failures.append(_search_failure(
                        stage="rerank",
                        source=f"{kind}_reranker",
                        source_type=kind,
                        code="RERANK_FAILED",
                        message=e,
                    ))
                    _assign_ranked(kind, _fallback_rank(kind))

        self._enrich_academic_pdf_text(
            ranked_papers,
            include_pdf_text=include_pdf_text,
            pdf_text_mode=pdf_text_mode,
            pdf_max_results=pdf_max_results,
            pdf_max_chars_per_result=pdf_max_chars_per_result,
            pdf_timeout_ms=pdf_timeout_ms,
        )
        if include_pdf_text:
            for paper in ranked_papers:
                if paper.pdf_error_code:
                    failures.append(_search_failure(
                        stage="pdf_enrichment",
                        source=paper.work_id or paper.doi or paper.title,
                        source_type="academic",
                        code=paper.pdf_error_code,
                        message=paper.pdf_error_message or paper.pdf_status,
                    ))
        evidence = self._build_evidence(ranked, ranked_papers, ranked_patents)
        search_boundary = None
        if trust_mode == "annotate":
            annotate_evidence(evidence)
            planned_sources = [provider.name for _, provider, _ in tasks]
            source_snapshot = {}
            for name in planned_sources:
                if name == "patent_es":
                    source_snapshot[name] = f"index-alias:{settings.patent_es_index}"
                elif name == "openalex_local":
                    source_snapshot[name] = "service-index:unspecified"
                else:
                    source_snapshot[name] = "provider-managed"
            search_boundary = build_search_boundary(
                query=plan.normalized_query,
                source_names=planned_sources,
                evidence=evidence,
                query_time=query_time,
                source_snapshot=source_snapshot,
                max_candidates=settings.per_provider_k * len(tasks),
            )
        answerability = _build_answerability(
            evidence,
            failures,
            expected_web=bool(active),
            expected_academic=plan.academic,
            expected_patent=plan.patent,
            include_pdf_text=include_pdf_text,
        )

        return SearchResponse(
            query=query,
            normalized_query=plan.normalized_query,
            rewritten_query=plan.rewritten_query,
            recency=plan.recency,
            time_sensitive=plan.time_sensitive,
            evidence=evidence,
            partial_failure=bool(failures),
            failures=failures,
            answerability=answerability,
            trust_mode=trust_mode,
            search_boundary=search_boundary,
            count=len(evidence),
            providers_used=used,
            reranker=text_scorer.name,
            ranking_profile=ranking.profile,
            rerank_threshold=ranking.threshold,
            rerank_threshold_mode=ranking.threshold_mode,
            ranking_warnings=list(ranking.warnings),
            elapsed_ms=int((time.time() - t0) * 1000),
        )


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "2026年人工智能最新进展"
    resp = SearchEngine().search(q)
    print(
        f"\n query={resp.query!r}  norm={resp.normalized_query!r}"
        + (f"  rewrite={resp.rewritten_query!r}" if resp.rewritten_query else "")
        + f"\n recency={resp.recency} time_sensitive={resp.time_sensitive}\n"
        f" sources={resp.providers_used}  reranker={resp.reranker}  "
        f"{resp.count} 条 evidence"
        + f"  {resp.elapsed_ms}ms\n"
        f" answerability={resp.answerability.status}/{resp.answerability.confidence} "
        f"partial_failure={resp.partial_failure}\n"
    )
    for gap in resp.answerability.gaps:
        print(f" gap[{gap.severity}] {gap.code}: {gap.message}")
    for failure in resp.failures:
        print(f" failure {failure.stage}/{failure.source} {failure.code}: {failure.message}")
    for i, e in enumerate(resp.evidence, 1):
        score = e.scores.relevance
        rs = f" score={score:.3f}" if score is not None else ""
        print(f"[{i}] {e.type} {e.title}  ({e.source} | {e.published_date}){rs}")
        print(f"    {e.url}")
        print(f"    {e.passage.text[:110]}\n")
