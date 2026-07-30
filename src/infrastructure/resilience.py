"""Deadline-aware retry and dependency circuit breakers."""
from __future__ import annotations

import random
import time
from collections import Counter
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Protocol, TypeVar

from src.application.ports.runtime import (
    Clock,
    Deadline,
    DeadlineExceededError,
)
from src.domain.errors import ExternalServiceError


T = TypeVar("T")


class ResilienceSettings(Protocol):
    resilience_max_attempts: int
    resilience_backoff_base_ms: int
    resilience_backoff_max_ms: int
    circuit_failure_threshold: int
    circuit_open_seconds: int


class CircuitOpenError(ExternalServiceError):
    def __init__(self, dependency: str, retry_after_seconds: float) -> None:
        super().__init__(
            provider=dependency,
            code="CIRCUIT_OPEN",
            recoverable=True,
            retry_after_seconds=max(0.0, retry_after_seconds),
        )


@dataclass
class _CircuitState:
    phase: str = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False


class CircuitBreaker:
    """Thread-safe closed/open/half-open circuit for one dependency."""

    def __init__(
        self,
        dependency: str,
        *,
        failure_threshold: int,
        open_seconds: float,
        monotonic: Callable[[], float],
    ) -> None:
        self.dependency = dependency
        self._failure_threshold = max(1, int(failure_threshold))
        self._open_seconds = max(0.001, float(open_seconds))
        self._monotonic = monotonic
        self._state = _CircuitState()
        self._lock = Lock()

    def acquire(self) -> None:
        with self._lock:
            if self._state.phase == "open":
                elapsed = self._monotonic() - self._state.opened_at
                if elapsed < self._open_seconds:
                    raise CircuitOpenError(
                        self.dependency,
                        self._open_seconds - elapsed,
                    )
                self._state.phase = "half_open"
                self._state.probe_in_flight = False
            if self._state.phase == "half_open":
                if self._state.probe_in_flight:
                    raise CircuitOpenError(self.dependency, self._open_seconds)
                self._state.probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._state = _CircuitState()

    def record_failure(self) -> None:
        with self._lock:
            if self._state.phase == "half_open":
                self._open_locked()
                return
            self._state.consecutive_failures += 1
            if self._state.consecutive_failures >= self._failure_threshold:
                self._open_locked()

    def record_ignored(self) -> None:
        """Release a half-open probe for non-transient/caller-side failures."""
        with self._lock:
            if self._state.phase == "half_open":
                self._state = _CircuitState()

    def _open_locked(self) -> None:
        self._state.phase = "open"
        self._state.opened_at = self._monotonic()
        self._state.probe_in_flight = False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            retry_after = 0.0
            if self._state.phase == "open":
                retry_after = max(
                    0.0,
                    self._open_seconds
                    - (self._monotonic() - self._state.opened_at),
                )
            return {
                "state": self._state.phase,
                "consecutive_failures": self._state.consecutive_failures,
                "retry_after_seconds": round(retry_after, 3),
            }


class ResilienceManager:
    """Apply bounded retries before recording one circuit outcome."""

    def __init__(
        self,
        settings: ResilienceSettings,
        clock: Clock,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._max_attempts = max(1, int(settings.resilience_max_attempts))
        self._base_delay = max(
            0.0, float(settings.resilience_backoff_base_ms) / 1000
        )
        self._max_delay = max(
            self._base_delay,
            float(settings.resilience_backoff_max_ms) / 1000,
        )
        self._failure_threshold = max(
            1, int(settings.circuit_failure_threshold)
        )
        self._open_seconds = max(0.001, float(settings.circuit_open_seconds))
        self._clock = clock
        self._sleeper = sleeper
        self._random_value = random_value
        self._breakers: dict[str, CircuitBreaker] = {}
        self._counters: dict[str, Counter[str]] = {}
        self._lock = Lock()

    def _breaker(self, dependency: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(dependency)
            if breaker is None:
                breaker = CircuitBreaker(
                    dependency,
                    failure_threshold=self._failure_threshold,
                    open_seconds=self._open_seconds,
                    monotonic=self._clock.monotonic,
                )
                self._breakers[dependency] = breaker
                self._counters[dependency] = Counter()
            return breaker

    def _increment(self, dependency: str, name: str) -> None:
        with self._lock:
            self._counters.setdefault(dependency, Counter())[name] += 1

    @staticmethod
    def _recoverable(exc: BaseException) -> bool:
        return (
            isinstance(exc, ExternalServiceError)
            and exc.recoverable
        )

    def _delay(self, attempt_index: int, exc: BaseException) -> float:
        cap = min(
            self._max_delay,
            self._base_delay * (2 ** attempt_index),
        )
        jittered = cap * (0.5 + 0.5 * max(0.0, min(1.0, self._random_value())))
        retry_after = getattr(exc, "retry_after_seconds", None)
        if retry_after is not None:
            return max(jittered, max(0.0, float(retry_after)))
        return jittered

    def call(
        self,
        dependency: str,
        operation: str,
        function: Callable[[], T],
        *,
        deadline: Deadline | None = None,
    ) -> T:
        dependency = dependency.strip() or "unknown"
        breaker = self._breaker(dependency)
        self._increment(dependency, "calls")
        try:
            breaker.acquire()
        except CircuitOpenError:
            self._increment(dependency, "circuit_rejections")
            raise

        try:
            for attempt in range(self._max_attempts):
                if deadline is not None and deadline.expired:
                    breaker.record_ignored()
                    self._increment(dependency, "deadline_exhausted")
                    raise DeadlineExceededError(
                        f"{operation} deadline exceeded"
                    )
                try:
                    result = function()
                except Exception as exc:
                    recoverable = self._recoverable(exc)
                    has_next = recoverable and attempt + 1 < self._max_attempts
                    if has_next:
                        delay = self._delay(attempt, exc)
                        if (
                            deadline is not None
                            and deadline.remaining_seconds() <= delay
                        ):
                            has_next = False
                    if has_next:
                        self._increment(dependency, "retries")
                        if delay > 0:
                            self._sleeper(delay)
                        continue
                    if recoverable:
                        breaker.record_failure()
                    else:
                        breaker.record_ignored()
                    self._increment(dependency, "failures")
                    raise
                breaker.record_success()
                self._increment(dependency, "successes")
                return result
        except BaseException:
            breaker.record_ignored()
            raise
        raise AssertionError("resilience attempt loop did not return or raise")

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            dependencies = list(self._breakers)
            counters = {
                name: dict(self._counters.get(name, Counter()))
                for name in dependencies
            }
        return {
            "max_attempts": self._max_attempts,
            "dependencies": {
                name: {
                    **self._breakers[name].snapshot(),
                    **counters[name],
                }
                for name in dependencies
            },
        }
