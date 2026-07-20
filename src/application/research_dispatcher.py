"""Dedicated background dispatcher for research jobs."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import Callable


class ResearchDispatcher:
    def __init__(self, runner: Callable[[str], None], *, max_workers: int) -> None:
        self._runner = runner
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="research-worker",
        )
        self._lock = RLock()
        self._futures: dict[str, Future[None]] = {}

    def submit(self, research_id: str) -> None:
        with self._lock:
            current = self._futures.get(research_id)
            if current is not None and not current.done():
                return
            future = self._executor.submit(self._runner, research_id)
            self._futures[research_id] = future
            future.add_done_callback(
                lambda _future, task_id=research_id: self._discard(task_id)
            )

    def _discard(self, research_id: str) -> None:
        with self._lock:
            self._futures.pop(research_id, None)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
