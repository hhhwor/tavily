"""Outbound DTO aliases used by REST, MCP and application services."""
from src.interfaces.public_models import (
    PublicResearchTaskEnvelope as ResearchTaskEnvelope,
    PublicSearchResponse as SearchResponse,
)

__all__ = ["ResearchTaskEnvelope", "SearchResponse"]
