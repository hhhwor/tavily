"""Compatibility exports for the cache adapter."""

from src.application.ports.cache import CacheBackend
from src.infrastructure.cache import InMemoryCache, build_cache

__all__ = ["CacheBackend", "InMemoryCache", "build_cache"]
