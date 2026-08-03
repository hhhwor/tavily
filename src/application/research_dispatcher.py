"""Bounded background dispatcher for the Research workload class."""
from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import BoundedSemaphore, RLock
from typing import Callable


class ResearchQueueFull(RuntimeError):
    code = "RESEARCH_QUEUE_FULL"

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(
            f"{self.code}: Research queue is full; retry later"
        )


@dataclass(frozen=True, slots=True)
class ResearchDispatcherStats:
    max_workers: int
    queue_capacity: int
    active: int
    queued: int
    reserved: int
    accepted_total: int
    rejected_total: int
    expired_total: int
    completed_total: int
    failed_total: int

    def as_dict(self) -> dict[str, int]:
        return {
            "max_workers": self.max_workers,
            "queue_capacity": self.queue_capacity,
            "active": self.active,
            "queued": self.queued,
            "reserved": self.reserved,
            "accepted_total": self.accepted_total,
            "rejected_total": self.rejected_total,
            "expired_total": self.expired_total,
            "completed_total": self.completed_total,
            "failed_total": self.failed_total,
        }


class DispatchReservation:
    def __init__(self, dispatcher: "ResearchDispatcher") -> None:
        self._dispatcher = dispatcher
        self._closed = False

    def submit(self, research_id: str) -> None:
        if self._closed:
            raise RuntimeError("dispatch reservation is already closed")
        self._closed = True
        self._dispatcher._submit_reserved(research_id)

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._dispatcher._release_reservation()


class ResearchDispatcher:
    """A bounded executor with admission reservation, queue TTL and metrics."""

    def __init__(
        self,
        runner: Callable[[str], None],
        *,
        max_workers: int,
        queue_capacity: int = 32,
        queue_ttl_ms: int = 120_000,
        retry_after_seconds: int = 1,
        on_expired: Callable[[str, int], None] | None = None,
        on_available: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if queue_capacity < 0:
            raise ValueError("queue_capacity must be non-negative")
        if queue_ttl_ms < 1:
            raise ValueError("queue_ttl_ms must be positive")
        self._runner = runner
        self._on_expired = on_expired
        self._on_available = on_available
        self._monotonic = monotonic
        self._max_workers = max_workers
        self._queue_capacity = queue_capacity
        self._queue_ttl_seconds = queue_ttl_ms / 1000
        self._retry_after_seconds = max(1, retry_after_seconds)
        # Capacity covers active workers plus bounded waiting work. Acquiring it
        # before persistence lets POST /research reject without orphaning a task.
        self._slots = BoundedSemaphore(max_workers + queue_capacity)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="research-worker",
        )
        self._lock = RLock()
        self._futures: dict[str, Future[None]] = {}
        self._scheduled_ids: set[str] = set()
        self._active = 0
        self._queued = 0
        self._reserved = 0
        self._accepted_total = 0
        self._rejected_total = 0
        self._expired_total = 0
        self._completed_total = 0
        self._failed_total = 0
        self._closed = False

    def reserve(self) -> DispatchReservation:
        with self._lock:
            if self._closed:
                raise RuntimeError("Research dispatcher is closed")
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._rejected_total += 1
            raise ResearchQueueFull(self._retry_after_seconds)
        with self._lock:
            if self._closed:
                self._slots.release()
                raise RuntimeError("Research dispatcher is closed")
            self._reserved += 1
        return DispatchReservation(self)

    def submit(self, research_id: str) -> None:
        with self._lock:
            if research_id in self._scheduled_ids:
                return
        reservation = self.reserve()
        try:
            reservation.submit(research_id)
        except BaseException:
            reservation.release()
            raise

    def _submit_reserved(self, research_id: str) -> None:
        enqueued_at = self._monotonic()
        with self._lock:
            if self._reserved <= 0:
                raise RuntimeError("missing dispatcher reservation")
            if research_id in self._scheduled_ids:
                self._reserved -= 1
                self._slots.release()
                return
            self._reserved -= 1
            self._queued += 1
            self._accepted_total += 1
            self._scheduled_ids.add(research_id)
        try:
            future = self._executor.submit(
                self._run,
                research_id,
                enqueued_at,
            )
        except BaseException:
            with self._lock:
                self._queued -= 1
                self._accepted_total -= 1
                self._scheduled_ids.discard(research_id)
            self._slots.release()
            raise
        with self._lock:
            self._futures[research_id] = future
        future.add_done_callback(
            lambda completed, task_id=research_id: self._discard(
                task_id,
                completed,
            )
        )

    def _release_reservation(self) -> None:
        with self._lock:
            if self._reserved <= 0:
                raise RuntimeError("missing dispatcher reservation")
            self._reserved -= 1
        self._slots.release()

    def _run(self, research_id: str, enqueued_at: float) -> None:
        with self._lock:
            self._queued -= 1
            self._active += 1
        failed = False
        try:
            queue_age = max(0.0, self._monotonic() - enqueued_at)
            if queue_age > self._queue_ttl_seconds:
                with self._lock:
                    self._expired_total += 1
                if self._on_expired is not None:
                    self._on_expired(research_id, int(queue_age * 1000))
                return
            self._runner(research_id)
        except BaseException:
            failed = True
            raise
        finally:
            with self._lock:
                self._active -= 1
                self._completed_total += 1
                if failed:
                    self._failed_total += 1
            self._slots.release()
            with self._lock:
                notify_available = not self._closed
            if notify_available and self._on_available is not None:
                self._on_available()

    def _discard(self, research_id: str, future: Future[None]) -> None:
        notify_available = False
        if future.cancelled():
            with self._lock:
                self._queued -= 1
                self._completed_total += 1
                notify_available = not self._closed
            self._slots.release()
        with self._lock:
            current = self._futures.get(research_id)
            if current is future:
                self._futures.pop(research_id, None)
            self._scheduled_ids.discard(research_id)
        if notify_available and self._on_available is not None:
            self._on_available()

    def contains(self, research_id: str) -> bool:
        with self._lock:
            return research_id in self._scheduled_ids

    def cancel(self, research_id: str) -> bool:
        """Remove queued work immediately; running work cancels cooperatively."""
        with self._lock:
            future = self._futures.get(research_id)
        return bool(future is not None and future.cancel())

    def stats(self) -> dict[str, int]:
        with self._lock:
            return ResearchDispatcherStats(
                max_workers=self._max_workers,
                queue_capacity=self._queue_capacity,
                active=self._active,
                queued=self._queued,
                reserved=self._reserved,
                accepted_total=self._accepted_total,
                rejected_total=self._rejected_total,
                expired_total=self._expired_total,
                completed_total=self._completed_total,
                failed_total=self._failed_total,
            ).as_dict()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
