"""
Rate limiter async con token bucket.

Garantiza no exceder max_per_second requests.
Compatible con asyncio, thread-safe via asyncio.Lock.
"""
from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """
    Token bucket rate limiter para controlar requests al CLOB REST API.

    Polymarket permite ~10 req/seg. Usamos 8 para margen de seguridad.

    Uso:
        limiter = RateLimiter(max_per_second=8)
        await limiter.acquire()  # bloquea hasta que haya token disponible
    """

    def __init__(self, max_per_second: float = 8.0) -> None:
        self.max_per_second = max_per_second
        self.min_interval = 1.0 / max_per_second
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0

    async def acquire(self) -> None:
        """Espera el tiempo necesario para respetar el rate limit."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            sleep_time = self.min_interval - elapsed

            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

            self._last_request_time = time.monotonic()

    async def acquire_many(self, count: int) -> None:
        """Adquiere múltiples tokens secuencialmente."""
        for _ in range(count):
            await self.acquire()
