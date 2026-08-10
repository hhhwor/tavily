"""Public in-process facade for the two supported capabilities."""
from __future__ import annotations

from typing import Any, Sequence

from src.application.commands import (
    ResearchCommand,
    ResearchFeedbackCommand,
    SearchCommand,
    SearchFilters,
)
from src.application.ports.research_store import StoredResearchArtifact
from src.application.research_service import ResearchService
from src.application.search_service import SearchService
from src.config import Settings
from src.domain.documents import DocumentKind
from src.interfaces.public_models import (
    PublicResearchTaskEnvelope,
    PublicSearchResponse,
    public_research_response,
    public_search_response,
)


class SearchEngine:
    """Expose only lightweight search and durable research task operations."""

    def __init__(
        self,
        *,
        settings: Settings,
        search_service: SearchService,
        research_service: ResearchService,
        providers: Sequence[Any],
        academic_provider: Any = None,
        patent_provider: Any = None,
        cache: Any = None,
        text_scorer: Any = None,
        ranking_service: Any = None,
        claim_verifier: Any = None,
        source_registry: Any = None,
    ) -> None:
        self.settings = settings
        self._search_service = search_service
        self._research_service = research_service
        self._ranking_service = ranking_service
        self.source_registry = source_registry
        self.providers = list(providers)
        self.academic_provider = academic_provider
        self.patent_provider = patent_provider
        self.cache = cache
        self.text_scorer = text_scorer
        self.claim_verifier = claim_verifier
        self._closed = False

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        source_types: tuple[DocumentKind, ...] | None = None,
        filters: SearchFilters | None = None,
    ) -> PublicSearchResponse:
        return self.execute(SearchCommand(
            query=query,
            limit=limit,
            source_types=source_types,
            filters=filters or SearchFilters(),
        ))

    def execute(self, command: SearchCommand) -> PublicSearchResponse:
        return public_search_response(self._search_service.execute(command))

    def start_research(
        self,
        command: ResearchCommand,
        *,
        idempotency_key: str,
    ) -> PublicResearchTaskEnvelope:
        return public_research_response(
            self._research_service.start(
                command,
                idempotency_key=idempotency_key,
            )
        )

    def get_research(
        self,
        research_id: str,
        *,
        detail: str = "standard",
    ) -> PublicResearchTaskEnvelope:
        return public_research_response(
            self._research_service.get(research_id, detail=detail)
        )

    def research_feedback(
        self,
        research_id: str,
        command: ResearchFeedbackCommand,
    ) -> PublicResearchTaskEnvelope:
        return public_research_response(
            self._research_service.feedback(research_id, command)
        )

    def cancel_research(
        self,
        research_id: str,
        *,
        task_revision: int | None = None,
    ) -> PublicResearchTaskEnvelope:
        return public_research_response(
            self._research_service.cancel(
                research_id,
                task_revision=task_revision,
            )
        )

    def get_research_artifact(
        self,
        research_id: str,
        artifact_id: str,
    ) -> StoredResearchArtifact:
        return self._research_service.get_artifact(
            research_id,
            artifact_id,
        )

    def close(self) -> None:
        """Release adapter resources; stores/executors are owned by Container."""
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
