"""Thread-safe in-process cache adapter."""
from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Callable

from src.application.ports.cache import CacheBackend


class InMemoryCache(CacheBackend):
    name = "memory"

    def __init__(
        self,
        capacity: int = 512,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._capacity = max(1, capacity)
        self._monotonic = monotonic
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            value, expire_at = item
            if self._monotonic() >= expire_at:
                del self._data[key]
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        with self._lock:
            if key in self._data:
                del self._data[key]
            elif len(self._data) >= self._capacity:
                self._data.popitem(last=False)
            self._data[key] = (value, self._monotonic() + ttl)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "backend": self.name,
                "size": len(self._data),
                "capacity": self._capacity,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }


def build_cache(
    backend: str = "memory",
    capacity: int = 512,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> CacheBackend:
    if backend and backend != "memory":
        print(f"[cache] 后端 '{backend}' 尚未实现,回退进程内缓存")
    return InMemoryCache(capacity=capacity, monotonic=monotonic)
