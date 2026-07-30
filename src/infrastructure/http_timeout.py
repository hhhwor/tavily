"""HTTP timeout construction shared by deadline-aware adapters."""
from __future__ import annotations

from urllib3.util import Timeout


def bounded_http_timeout(
    configured_seconds: float,
    budget_seconds: float | None = None,
) -> Timeout:
    """Return a urllib3 total timeout capped by the remaining request budget.

    ``requests`` accepts urllib3 ``Timeout`` instances directly. Supplying a
    total timeout is important: a scalar requests timeout is independently
    applied to connection and socket reads and therefore is not an end-to-end
    bound for one HTTP exchange.
    """
    configured = float(configured_seconds)
    if configured <= 0:
        raise ValueError("configured HTTP timeout must be greater than zero")
    effective = configured
    if budget_seconds is not None:
        budget = float(budget_seconds)
        if budget <= 0:
            raise ValueError("HTTP timeout budget must be greater than zero")
        effective = min(configured, budget)
    return Timeout(total=effective, connect=effective)
