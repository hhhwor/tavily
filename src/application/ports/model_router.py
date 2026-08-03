"""Privacy-aware model routing boundary for Research."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from src.domain.research import ResearchPrivacy


ModelRoute = Literal["configured", "local", "disabled"]


@dataclass(frozen=True, slots=True)
class ResolvedModelRoute:
    rewrite: ModelRoute
    rerank: ModelRoute
    verify: ModelRoute
    synthesis: ModelRoute
    allow_external_models: bool

    @property
    def name(self) -> str:
        return (
            "standard_external_allowed"
            if self.allow_external_models
            else "restricted_local_only"
        )


class ModelRouter(Protocol):
    def resolve(
        self,
        *,
        privacy: ResearchPrivacy,
        policy_id: str,
    ) -> ResolvedModelRoute: ...
