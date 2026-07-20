"""Outbound DTO aliases used by REST, MCP and application services."""
from src.domain.research import ResearchTaskEnvelope
from src.domain.search_api import SearchResponse

__all__ = ["ResearchTaskEnvelope", "SearchResponse"]
