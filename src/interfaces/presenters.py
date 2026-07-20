"""Transport projections are intentionally identity mappings in v1."""
from __future__ import annotations

from typing import Any

from src.domain.search_api import SearchResponse


class McpSearchPresenter:
    @staticmethod
    def present(response: SearchResponse) -> dict[str, Any]:
        return response.model_dump(mode="json")

    @staticmethod
    def restore(data: dict[str, Any]) -> SearchResponse:
        return SearchResponse.model_validate(data)
