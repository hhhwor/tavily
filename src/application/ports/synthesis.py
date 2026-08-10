"""Port for optional structured Research synthesis."""
from __future__ import annotations

from typing import Protocol

from src.application.ports.runtime import Deadline
from src.application.research_execution import CancellationToken
from src.domain.synthesis import SynthesisGatewayResult, SynthesisRequest


class SynthesisGateway(Protocol):
    name: str
    is_external: bool

    def synthesize(
        self,
        request: SynthesisRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken,
    ) -> SynthesisGatewayResult: ...
