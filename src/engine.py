"""兼容搜索门面；真实用例编排位于 ``src.application``。"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from src.application.commands import SearchCommand
from src.application.ports.pdf_text import PdfTextGateway
from src.application.search_service import SearchService
from src.application.verify_service import VerifyService
from src.config import Settings
from src.models import (
    CandidateClaim,
    Evidence,
    PdfTextResponse,
    SearchBoundary,
    SearchResponse,
    VerifyResponse,
)


class SearchEngine:
    """保留旧公开签名的薄门面，由 composition root 注入应用用例。"""

    def __init__(
        self,
        *,
        settings: Settings,
        search_service: SearchService,
        verify_service: VerifyService,
        pdf_gateway: PdfTextGateway,
        providers: Sequence[Any],
        academic_provider: Any = None,
        patent_provider: Any = None,
        cache: Any = None,
        text_scorer: Any = None,
        ranking_service: Any = None,
        source_registry: Any = None,
    ) -> None:
        self.settings = settings
        self._search_service = search_service
        self._verify_service = verify_service
        self._pdf_gateway = pdf_gateway
        self._ranking_service = ranking_service
        self.source_registry = source_registry

        # 兼容健康检查与既有运维读取；搜索编排不再依赖这些属性。
        self.providers = list(providers)
        self.academic_provider = academic_provider
        self.patent_provider = patent_provider
        self.cache = cache
        self.text_scorer = text_scorer
        self.claim_verifier = verify_service.verifier
        self._closed = False

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
        return self.execute(SearchCommand(
            query=query,
            top_k=top_k,
            include_academic=include_academic,
            include_patent=include_patent,
            rerank_enabled=rerank_enabled,
            rerank_backend=rerank_backend,
            rerank_model=rerank_model,
            rerank_threshold=rerank_threshold,
            fusion_enabled=fusion_enabled,
            ranking_profile=ranking_profile,
            rerank_threshold_mode=rerank_threshold_mode,
            rewrite_enabled=rewrite_enabled,
            trust_mode=trust_mode,
            include_pdf_text=include_pdf_text,
            pdf_text_mode=pdf_text_mode,
            pdf_max_results=pdf_max_results,
            pdf_max_chars_per_result=pdf_max_chars_per_result,
            pdf_timeout_ms=pdf_timeout_ms,
        ))

    def execute(self, command: SearchCommand) -> SearchResponse:
        """Public use-case entry shared by REST, MCP and in-process clients."""
        return self._search_service.execute(command)

    def verify_claims(
        self,
        query: str,
        claims: Sequence[CandidateClaim],
        evidence: Sequence[Evidence],
        *,
        profile: str = "general",
        search_boundary: Optional[SearchBoundary] = None,
    ) -> VerifyResponse:
        return self._verify_service.verify(
            query,
            claims,
            evidence,
            profile=profile,
            search_boundary=search_boundary,
        )

    def get_pdf_text(
        self,
        work_id: str,
        cursor: Optional[str] = None,
        max_chars: Optional[int] = None,
    ) -> PdfTextResponse:
        return self._pdf_gateway.read_page(
            work_id,
            cursor=cursor,
            max_chars=max_chars,
        )

    def close(self) -> None:
        """释放 Engine 自有适配器；共享 HTTP/Executor 仍由 Container 关闭。"""
        if self._closed:
            return
        self._closed = True
        resources = [
            self._ranking_service,
            self.text_scorer,
            self.claim_verifier,
            getattr(self.claim_verifier, "classifier", None),
            *self.providers,
            self.academic_provider,
            self.patent_provider,
            self.cache,
        ]
        closed: set[int] = set()
        first_error: BaseException | None = None
        for resource in resources:
            if resource is None or id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as exc:
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error


if __name__ == "__main__":
    import sys

    from src.bootstrap import build_container

    query = sys.argv[1] if len(sys.argv) > 1 else "2026年人工智能最新进展"
    container = build_container(include_mcp=False)
    try:
        response = container.engine.search(query)
    finally:
        container.close()
    print(response.model_dump_json(indent=2))
