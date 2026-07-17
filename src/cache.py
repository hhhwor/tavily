"""缓存服务 —— 对 provider 召回结果缓存,避免重复调用搜索源 API。

设计:
- `CacheBackend` 抽象接口,当前提供进程内 `InMemoryCache`(LRU + 按 key TTL,线程安全)。
- 未来换 Redis 只需新增一个 `CacheBackend` 实现并在 `build_cache` 加分支,调用方无感。

注意:本模块只负责"存什么取什么",调用方(RecallCoordinator)负责
- 决定哪些查询可缓存(如时效查询不缓存)、key 怎么构造、存取时深拷贝防对象污染。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from threading import Lock
from typing import Any, Optional


class CacheBackend(ABC):
    """缓存后端接口。实现需保证线程安全。"""

    name: str = "base"

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """命中且未过期返回值,否则返回 None。"""
        raise NotImplementedError

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int) -> None:
        """写入,ttl 秒后过期。"""
        raise NotImplementedError

    def stats(self) -> dict:
        return {}


class InMemoryCache(CacheBackend):
    """进程内 LRU + 按 key TTL 缓存(线程安全)。

    单进程 uvicorn 完全够用;进程重启即清空。容量满时淘汰最久未用项。
    """

    name = "memory"

    def __init__(self, capacity: int = 512):
        self._data: "OrderedDict[str, tuple[Any, float]]" = OrderedDict()  # key -> (value, expire_at)
        self._capacity = max(1, capacity)
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            value, expire_at = item
            if time.time() >= expire_at:
                del self._data[key]
                self._misses += 1
                return None
            self._data.move_to_end(key)  # LRU:命中移到末尾
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        with self._lock:
            if key in self._data:
                del self._data[key]
            elif len(self._data) >= self._capacity:
                self._data.popitem(last=False)  # 淘汰最久未用
            self._data[key] = (value, time.time() + ttl)

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


def build_cache(backend: str = "memory", capacity: int = 512) -> CacheBackend:
    """缓存工厂。预留 redis 等后端;未知/未实现的后端回退 InMemoryCache。"""
    if backend and backend != "memory":
        # 预留:未来在此新增 RedisCache(CacheBackend) 等分支
        print(f"[cache] 后端 '{backend}' 尚未实现,回退进程内缓存")
    return InMemoryCache(capacity=capacity)
