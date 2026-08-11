from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


K = TypeVar("K")
V = TypeVar("V")


@dataclass(slots=True)
class _Entry(Generic[V]):
    value: V
    expires_at: float


class TTLCache(Generic[K, V]):
    def __init__(self) -> None:
        self._entries: dict[K, _Entry[V]] = {}
        self._locks: dict[K, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: K, value: V, ttl: float) -> None:
        self._entries[key] = _Entry(value, time.monotonic() + ttl)

    def delete(self, key: K) -> None:
        self._entries.pop(key, None)

    async def get_or_set(
        self,
        key: K,
        factory: Callable[[], Awaitable[V]],
        ttl: float,
    ) -> V:
        cached = self.get(key)
        if cached is not None:
            return cached

        lock = await self._lock_for(key)
        async with lock:
            cached = self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            self.set(key, value, ttl)
            return value

    async def _lock_for(self, key: K) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())
