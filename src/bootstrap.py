"""应用唯一 composition root：读取配置并装配、管理所有进程资源。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import requests

from src.application.answerability import AnswerabilityPolicy
from src.application.evidence_assembler import EvidenceAssembler
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.search_service import SearchService
from src.application.trust_annotator import TrustAnnotator
from src.application.verify_service import VerifyService
from src.cache import build_cache
from src.config import Settings
from src.engine import SearchEngine
from src.infrastructure.openalex_pdf import OpenAlexPdfGateway
from src.pipeline.rerank import Reranker, build_text_scorer
from src.providers.base import SearchProvider
from src.trust import build_claim_verifier


def _web_providers(
    settings: Settings,
    http: requests.Session,
) -> list[SearchProvider]:
    providers: list[SearchProvider] = []
    if settings.tencent_secret_id and settings.tencent_secret_key:
        from src.providers.tencent import TencentSearchProvider

        providers.append(TencentSearchProvider(
            secret_id=settings.tencent_secret_id,
            secret_key=settings.tencent_secret_key,
            timeout=settings.provider_timeout,
            http_session=http,
        ))
    if settings.qianfan_api_key:
        from src.providers.baidu import BaiduSearchProvider

        providers.append(BaiduSearchProvider(
            api_key=settings.qianfan_api_key,
            timeout=settings.provider_timeout,
            http_session=http,
        ))
    if settings.serpapi_api_key:
        from src.providers.serpapi import SerpApiProvider

        providers.append(SerpApiProvider(
            api_key=settings.serpapi_api_key,
            timeout=settings.provider_timeout,
            http_session=http,
        ))
    return providers


def _academic_provider(
    settings: Settings,
    http: requests.Session,
) -> Optional[SearchProvider]:
    if not settings.academic_enabled:
        return None
    from src.providers.openalex import OpenAlexProvider

    return OpenAlexProvider(
        base_url=settings.openalex_api_url,
        api_key=settings.openalex_api_key,
        per_page=settings.openalex_per_page,
        timeout=settings.provider_timeout,
        http_session=http,
    )


def _patent_provider(
    settings: Settings,
    http: requests.Session,
) -> Optional[SearchProvider]:
    if not settings.patent_enabled:
        return None
    from src.providers.patent_es import PatentEsProvider

    return PatentEsProvider(
        base_url=settings.patent_es_url,
        index=settings.patent_es_index,
        timeout=settings.provider_timeout,
        verify_tls=settings.patent_es_verify_tls,
        per_page=settings.patent_es_per_page,
        http_session=http,
    )


def _scorer_factory(
    settings: Settings,
    http: requests.Session,
):
    def build(enabled: bool, backend: str, model: str) -> Reranker:
        return build_text_scorer(
            enabled=enabled,
            backend=backend,
            model_name=model,
            cache_dir=settings.rerank_cache_dir,
            device=settings.rerank_device,
            chunk_max_chars=settings.chunk_max_chars,
            chunk_overlap=settings.chunk_overlap,
            siliconflow_api_key=settings.siliconflow_api_key,
            siliconflow_base_url=settings.siliconflow_base_url,
            http_session=http,
        )

    return build


def _claim_verifier(settings: Settings, http: requests.Session):
    return build_claim_verifier(
        backend=settings.trust_verify_backend,
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model=settings.trust_verify_model,
        timeout=settings.trust_verify_timeout,
        max_claims=settings.trust_verify_max_claims,
        max_evidence_per_claim=settings.trust_verify_max_evidence,
        http_session=http,
    )


def _source_snapshot(settings: Settings, source: str) -> str:
    if source == "patent_es":
        return f"index-alias:{settings.patent_es_index}"
    if source == "openalex_local":
        return "service-index:unspecified"
    return "provider-managed"


@dataclass
class Container:
    """单个应用实例的运行时资源；不与其他 app 共享可变单例。"""

    settings: Settings
    engine: SearchEngine
    http_session: requests.Session
    executor: ThreadPoolExecutor
    mcp: Any = None
    mcp_app: Any = None
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def mcp_available(self) -> bool:
        return self.mcp is not None and self.mcp_app is not None

    @property
    def closed(self) -> bool:
        return self._closed

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator["Container"]:
        if self._closed:
            raise RuntimeError("Container 已关闭；请通过 container_factory 创建新的运行时")
        try:
            if self.mcp is not None:
                async with self.mcp.session_manager.run():
                    yield self
            else:
                yield self
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.engine.close()
        finally:
            try:
                self.executor.shutdown(wait=True, cancel_futures=True)
            finally:
                self.http_session.close()


def build_container(
    settings: Optional[Settings] = None,
    *,
    include_mcp: bool = True,
) -> Container:
    """创建完整运行时；调用方必须进入 ``Container.lifespan`` 或显式 close。"""
    config = settings or Settings.from_env()
    http = requests.Session()
    executor = ThreadPoolExecutor(
        max_workers=config.executor_max_workers,
        thread_name_prefix="search-worker",
    )
    engine: Optional[SearchEngine] = None
    scorer: Any = None
    verifier: Any = None
    cache: Any = None
    providers: list[SearchProvider] = []
    academic_provider: Optional[SearchProvider] = None
    patent_provider: Optional[SearchProvider] = None
    ranking_service: Optional[RankingService] = None
    try:
        scorer_factory = _scorer_factory(config, http)
        scorer = scorer_factory(
            config.rerank_enabled,
            config.rerank_backend,
            config.rerank_model,
        )
        verifier = _claim_verifier(config, http)
        cache = (
            build_cache(config.cache_backend, config.cache_max_size)
            if config.cache_enabled
            else None
        )
        providers = _web_providers(config, http)
        academic_provider = _academic_provider(config, http)
        patent_provider = _patent_provider(config, http)
        ranking_service = RankingService(
            config,
            scorer,
            scorer_factory,
            executor,
        )
        pdf_gateway = OpenAlexPdfGateway(config, http, executor)
        search_service = SearchService(
            query_planner=QueryPlanner(config, http),
            recall=RecallCoordinator(
                config,
                providers,
                academic_provider,
                patent_provider,
                cache,
                executor,
                snapshot_resolver=lambda source: _source_snapshot(config, source),
            ),
            ranking=ranking_service,
            pdf_gateway=pdf_gateway,
            evidence_assembler=EvidenceAssembler(),
            trust_annotator=TrustAnnotator(
                lambda source: _source_snapshot(config, source)
            ),
            answerability=AnswerabilityPolicy(),
            provider_names=[provider.name for provider in providers],
            academic_available=academic_provider is not None,
            patent_available=patent_provider is not None,
        )
        verify_service = VerifyService(verifier)
        engine = SearchEngine(
            settings=config,
            search_service=search_service,
            verify_service=verify_service,
            pdf_gateway=pdf_gateway,
            providers=providers,
            academic_provider=academic_provider,
            patent_provider=patent_provider,
            cache=cache,
            text_scorer=scorer,
            ranking_service=ranking_service,
        )

        mcp = None
        mcp_app = None
        if include_mcp and config.mcp_enabled:
            try:
                from src.mcp_server import build_mcp
            except (ImportError, ModuleNotFoundError):
                if config.mcp_required:
                    raise
                print("[bootstrap] MCP 依赖不可用，降级为仅 REST")
            else:
                mcp = build_mcp(engine, config)
                mcp_app = mcp.streamable_http_app()

        return Container(
            settings=config,
            engine=engine,
            http_session=http,
            executor=executor,
            mcp=mcp,
            mcp_app=mcp_app,
        )
    except BaseException:
        try:
            if engine is not None:
                try:
                    engine.close()
                except BaseException:
                    pass
            else:
                resources = [
                    ranking_service,
                    scorer,
                    verifier,
                    getattr(verifier, "classifier", None),
                    cache,
                    *providers,
                    academic_provider,
                    patent_provider,
                ]
                closed: set[int] = set()
                for resource in resources:
                    if resource is None or id(resource) in closed:
                        continue
                    closed.add(id(resource))
                    close = getattr(resource, "close", None)
                    if callable(close):
                        try:
                            close()
                        except BaseException:
                            pass
        finally:
            try:
                try:
                    executor.shutdown(wait=True, cancel_futures=True)
                except BaseException:
                    pass
            finally:
                try:
                    http.close()
                except BaseException:
                    pass
        raise
