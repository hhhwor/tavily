"""应用唯一 composition root：读取配置并装配、管理所有进程资源。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import requests

from src.application.answerability import AnswerabilityPolicy
from src.application.discovery_service import DiscoveryService
from src.application.evidence_assembler import EvidenceAssembler
from src.application.model_router import PrivacyAwareModelRouter
from src.application.patent_document_reader import PatentDocumentReader
from src.application.query_planner import QueryPlanner
from src.application.ranking_service import RankingService
from src.application.recall import RecallCoordinator
from src.application.search_service import SearchService
from src.application.research_dispatcher import ResearchDispatcher
from src.application.research_service import ResearchService
from src.application.source_registry import SourceRegistry
from src.application.trust_annotator import TrustAnnotator
from src.application.verify_service import VerifyService
from src.config import Settings
from src.engine import SearchEngine
from src.infrastructure.cache import InMemoryCache, build_cache
from src.infrastructure.openalex_pdf import OpenAlexPdfGateway
from src.infrastructure.patent_es_fulltext import PatentEsFullTextGateway
from src.infrastructure.query_rewriter import SiliconFlowQueryRewriter
from src.infrastructure.siliconflow_embedding_intent_classifier import (
    SiliconFlowEmbeddingIntentClassifier,
)
from src.infrastructure.siliconflow_intent_classifier import (
    SiliconFlowIntentClassifier,
)
from src.infrastructure.resilience import ResilienceManager
from src.infrastructure.runtime import SystemClock
from src.infrastructure.sqlite_research_store import SqliteResearchStore
from src.infrastructure.sqlite_seed_store import SqliteSearchSeedStore
from src.infrastructure.siliconflow_synthesis import SiliconFlowSynthesisGateway
from src.providers.base import SearchProvider
from src.ranking.factory import build_text_scorer
from src.ranking.ports import Reranker
from src.trust import build_claim_verifier


def _shutdown_executors(*executors: ThreadPoolExecutor) -> None:
    first_error: BaseException | None = None
    closed: set[int] = set()
    for executor in executors:
        if id(executor) in closed:
            continue
        closed.add(id(executor))
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except BaseException as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


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
    if settings.doubao_api_key:
        from src.providers.doubao import DoubaoSearchProvider

        providers.append(DoubaoSearchProvider(
            api_key=settings.doubao_api_key,
            timeout=settings.provider_timeout,
            uvx_path=settings.doubao_uvx_path,
        ))
    if (
        settings.aliyun_web_search_enabled
        and settings.aliyun_access_key_id
        and settings.aliyun_access_key_secret
    ):
        from src.providers.aliyun import AliyunWebSearchProvider

        providers.append(AliyunWebSearchProvider(
            access_key_id=settings.aliyun_access_key_id,
            access_key_secret=settings.aliyun_access_key_secret,
            timeout=settings.provider_timeout,
            search_type=settings.aliyun_web_search_type,
            region=settings.aliyun_web_search_region,
            http_session=http,
        ))
    if settings.serpapi_enabled and settings.serpapi_api_key:
        from src.providers.serpapi import SerpApiProvider

        providers.append(SerpApiProvider(
            api_key=settings.serpapi_api_key,
            timeout=settings.provider_timeout,
            http_session=http,
        ))
    if settings.fy_law_mcp_enabled:
        from src.providers.fy_law_mcp import FyLawMcpProvider

        providers.append(FyLawMcpProvider(
            endpoint=settings.fy_law_mcp_url,
            token=settings.fy_law_mcp_token,
            token_file=settings.fy_law_mcp_token_file,
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


def _claim_verifier(
    settings: Settings,
    http: requests.Session,
    clock: SystemClock,
):
    return build_claim_verifier(
        backend=settings.trust_verify_backend,
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model=settings.trust_verify_model,
        timeout=settings.trust_verify_timeout,
        max_claims=settings.trust_verify_max_claims,
        max_evidence_per_claim=settings.trust_verify_max_evidence,
        http_session=http,
        monotonic=clock.monotonic,
    )


@dataclass
class Container:
    """单个应用实例的运行时资源；不与其他 app 共享可变单例。"""

    settings: Settings
    engine: SearchEngine
    http_session: requests.Session
    recall_executor: ThreadPoolExecutor
    ranking_executor: ThreadPoolExecutor
    research_recall_executor: ThreadPoolExecutor
    research_ranking_executor: ThreadPoolExecutor
    pdf_executor: ThreadPoolExecutor
    resilience: ResilienceManager
    research_dispatcher: ResearchDispatcher
    seed_store: SqliteSearchSeedStore
    research_store: SqliteResearchStore
    mcp: Any = None
    mcp_app: Any = None
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def mcp_available(self) -> bool:
        return self.mcp is not None and self.mcp_app is not None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def executor(self) -> ThreadPoolExecutor:
        """Backward-compatible alias for the recall executor."""
        return self.recall_executor

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
            self.research_dispatcher.close()
        finally:
            try:
                self.engine.close()
            finally:
                try:
                    self.seed_store.close()
                finally:
                    try:
                        self.research_store.close()
                    finally:
                        try:
                            _shutdown_executors(
                                self.recall_executor,
                                self.ranking_executor,
                                self.research_recall_executor,
                                self.research_ranking_executor,
                                self.pdf_executor,
                            )
                        finally:
                            self.http_session.close()


def build_container(
    settings: Optional[Settings] = None,
    *,
    include_mcp: bool = True,
) -> Container:
    """创建完整运行时；调用方必须进入 ``Container.lifespan`` 或显式 close。"""
    config = settings or Settings.from_env()
    clock = SystemClock()
    resilience = ResilienceManager(config, clock)
    http = requests.Session()
    recall_executor = ThreadPoolExecutor(
        max_workers=config.executor_max_workers,
        thread_name_prefix="search-recall",
    )
    ranking_executor = ThreadPoolExecutor(
        max_workers=config.ranking_executor_max_workers,
        thread_name_prefix="search-ranking",
    )
    research_recall_executor = ThreadPoolExecutor(
        max_workers=config.research_recall_max_workers,
        thread_name_prefix="research-recall",
    )
    research_ranking_executor = ThreadPoolExecutor(
        max_workers=config.research_ranking_max_workers,
        thread_name_prefix="research-ranking",
    )
    pdf_executor = ThreadPoolExecutor(
        max_workers=config.pdf_executor_max_workers,
        thread_name_prefix="research-pdf",
    )
    engine: Optional[SearchEngine] = None
    scorer: Any = None
    verifier: Any = None
    cache: Any = None
    providers: list[SearchProvider] = []
    academic_provider: Optional[SearchProvider] = None
    patent_provider: Optional[SearchProvider] = None
    ranking_service: Optional[RankingService] = None
    seed_store: Optional[SqliteSearchSeedStore] = None
    research_store: Optional[SqliteResearchStore] = None
    research_dispatcher: Optional[ResearchDispatcher] = None
    try:
        scorer_factory = _scorer_factory(config, http)
        scorer = scorer_factory(
            config.rerank_enabled,
            config.rerank_backend,
            config.rerank_model,
        )
        verifier = _claim_verifier(config, http, clock)
        cache = (
            build_cache(
                config.cache_backend,
                config.cache_max_size,
                monotonic=clock.monotonic,
            )
            if config.cache_enabled
            else None
        )
        providers = _web_providers(config, http)
        academic_provider = _academic_provider(config, http)
        patent_provider = _patent_provider(config, http)
        registry = SourceRegistry([
            *providers,
            *([academic_provider] if academic_provider is not None else []),
            *([patent_provider] if patent_provider is not None else []),
        ])
        ranking_service = RankingService(
            config,
            scorer,
            scorer_factory,
            ranking_executor,
            clock=clock,
            resilience=resilience,
            research_executor=research_ranking_executor,
        )
        pdf_gateway = OpenAlexPdfGateway(
            config, http, pdf_executor, monotonic=clock.monotonic
        )
        query_rewriter = SiliconFlowQueryRewriter(
            config.siliconflow_api_key,
            config.siliconflow_base_url,
            config.rewrite_model,
            cache=InMemoryCache(
                config.rewrite_cache_size,
                monotonic=clock.monotonic,
            ),
            http_session=http,
        )
        intent_classifier = None
        if config.intent_classifier_enabled and config.siliconflow_api_key:
            intent_cache = InMemoryCache(
                config.intent_classifier_cache_size,
                monotonic=clock.monotonic,
            )
            if config.intent_classifier_backend == "embedding":
                intent_classifier = SiliconFlowEmbeddingIntentClassifier(
                    config.siliconflow_api_key,
                    config.siliconflow_base_url,
                    config.intent_classifier_model,
                    cache=intent_cache,
                    http_session=http,
                    cache_ttl=config.intent_classifier_cache_ttl,
                    timeout=config.intent_classifier_timeout,
                    academic_threshold=(
                        config.intent_embedding_academic_threshold
                    ),
                    patent_threshold=config.intent_embedding_patent_threshold,
                    legal_threshold=config.intent_embedding_legal_threshold,
                )
            else:
                intent_classifier = SiliconFlowIntentClassifier(
                    config.siliconflow_api_key,
                    config.siliconflow_base_url,
                    config.intent_classifier_model,
                    cache=intent_cache,
                    http_session=http,
                    cache_ttl=config.intent_classifier_cache_ttl,
                    timeout=config.intent_classifier_timeout,
                )
        query_planner = QueryPlanner(
            config,
            query_rewriter,
            intent_classifier=intent_classifier,
            resilience=resilience,
        )
        recall = RecallCoordinator(
            config,
            registry,
            cache,
            recall_executor,
            clock=clock.now,
            resilience=resilience,
            research_executor=research_recall_executor,
        )
        discovery = DiscoveryService(
            query_planner=query_planner,
            recall=recall,
            ranking=ranking_service,
            source_registry=registry,
            clock=clock,
            deadline_ms=config.search_deadline_ms,
        )
        seed_store = SqliteSearchSeedStore(config.state_db_path)
        research_store = SqliteResearchStore(config.state_db_path)
        evidence_assembler = EvidenceAssembler()
        trust_annotator = TrustAnnotator(registry.snapshot_for)
        search_service = SearchService(
            discovery=discovery,
            evidence_assembler=evidence_assembler,
            trust_annotator=trust_annotator,
            answerability=AnswerabilityPolicy(),
            seed_store=seed_store,
            clock=clock,
            deadline_ms=config.search_deadline_ms,
            seed_ttl_seconds=config.search_seed_ttl_seconds,
        )
        verify_service = VerifyService(verifier)
        synthesis_gateway = (
            SiliconFlowSynthesisGateway(
                api_key=config.siliconflow_api_key,
                base_url=config.siliconflow_base_url,
                model=config.research_synthesis_model,
                timeout=config.research_synthesis_timeout,
                http_session=http,
            )
            if config.research_synthesis_enabled else None
        )
        research_document_readers = {}
        if config.patent_fulltext_enabled:
            research_document_readers["patent"] = PatentDocumentReader(
                PatentEsFullTextGateway(
                    base_url=config.patent_fulltext_url,
                    index=config.patent_fulltext_index,
                    http_session=http,
                    timeout_seconds=config.provider_timeout,
                    verify_tls=config.patent_fulltext_verify_tls,
                )
            )
        research_service = ResearchService(
            seed_store=seed_store,
            task_store=research_store,
            discovery=discovery,
            evidence_assembler=evidence_assembler,
            trust_annotator=trust_annotator,
            pdf_gateway=pdf_gateway,
            verify_service=verify_service,
            clock=clock,
            model_router=PrivacyAwareModelRouter(
                local_verification_available=True,
                local_reranking_available=(
                    config.rerank_backend in {"bge", "flashrank"}
                ),
            ),
            document_readers=research_document_readers,
            synthesis_gateway=synthesis_gateway,
            artifact_retention_seconds=(
                config.research_artifact_retention_seconds
            ),
        )
        research_dispatcher = ResearchDispatcher(
            research_service.run,
            max_workers=config.research_max_workers,
            queue_capacity=config.research_queue_capacity,
            queue_ttl_ms=config.research_queue_ttl_ms,
            retry_after_seconds=config.research_queue_retry_after_seconds,
            on_expired=research_service.expire_queued,
            on_available=research_service.recover_pending,
            monotonic=clock.monotonic,
        )
        research_service.attach_dispatcher(research_dispatcher)
        engine = SearchEngine(
            settings=config,
            search_service=search_service,
            research_service=research_service,
            providers=providers,
            academic_provider=academic_provider,
            patent_provider=patent_provider,
            cache=cache,
            text_scorer=scorer,
            ranking_service=ranking_service,
            claim_verifier=verifier,
            source_registry=registry,
        )
        research_service.recover_pending()

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
            recall_executor=recall_executor,
            ranking_executor=ranking_executor,
            research_recall_executor=research_recall_executor,
            research_ranking_executor=research_ranking_executor,
            pdf_executor=pdf_executor,
            resilience=resilience,
            research_dispatcher=research_dispatcher,
            seed_store=seed_store,
            research_store=research_store,
            mcp=mcp,
            mcp_app=mcp_app,
        )
    except BaseException:
        try:
            if engine is not None:
                try:
                    if research_dispatcher is not None:
                        research_dispatcher.close()
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
                    seed_store,
                    research_store,
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
                    _shutdown_executors(
                        recall_executor,
                        ranking_executor,
                        research_recall_executor,
                        research_ranking_executor,
                        pdf_executor,
                    )
                except BaseException:
                    pass
            finally:
                try:
                    http.close()
                except BaseException:
                    pass
        raise
