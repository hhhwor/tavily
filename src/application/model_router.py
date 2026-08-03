"""Server-owned privacy routing for rewrite, rerank and verification models."""
from __future__ import annotations

from src.application.ports.model_router import ResolvedModelRoute
from src.domain.research import ResearchPrivacy


class PrivacyPolicyUnsatisfiable(ValueError):
    code = "PRIVACY_POLICY_UNSATISFIABLE"


class PrivacyAwareModelRouter:
    def __init__(
        self,
        *,
        local_verification_available: bool = True,
        local_reranking_available: bool = False,
    ) -> None:
        self._local_verification_available = local_verification_available
        self._local_reranking_available = local_reranking_available

    def resolve(
        self,
        *,
        privacy: ResearchPrivacy,
        policy_id: str,
    ) -> ResolvedModelRoute:
        external_allowed = (
            privacy.mode == "standard" and privacy.allow_external_models
        )
        if external_allowed:
            return ResolvedModelRoute(
                rewrite="configured",
                rerank="configured",
                verify="configured",
                synthesis="configured",
                allow_external_models=True,
            )
        if not self._local_verification_available:
            raise PrivacyPolicyUnsatisfiable(
                f"{PrivacyPolicyUnsatisfiable.code}: policy {policy_id} "
                "没有本地合规验证路径"
            )
        return ResolvedModelRoute(
            rewrite="disabled",
            rerank="local" if self._local_reranking_available else "disabled",
            verify="local",
            synthesis="disabled",
            allow_external_models=False,
        )
