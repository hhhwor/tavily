"""Publicly translated Research request errors."""


class ResearchRequestError(ValueError):
    """The requested policy/scope cannot be accepted as submitted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "RESEARCH_REQUEST_INVALID",
    ) -> None:
        self.code = code
        super().__init__(message)
