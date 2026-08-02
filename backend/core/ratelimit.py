"""Per-key rate limiting.

A sliding window log rather than a fixed window: a fixed window lets a caller
send 2x the limit across a window boundary, which for an endpoint that pins a
CPU core for seconds is the difference between a limit and a suggestion.

Two backends behind one interface. In-memory is the default and needs no
infrastructure, but its counters are per-process -- run two instances behind a
load balancer and each enforces the limit separately. Set CLOUDLEAK_REDIS_URL
to share state across instances.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Protocol

logger = logging.getLogger("cloudleak.ratelimit")


@dataclass(frozen=True)
class RateLimitVerdict:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    limit: int


class RateLimiter(Protocol):
    async def check(self, key: str) -> RateLimitVerdict: ...
    async def close(self) -> None: ...


class InMemoryRateLimiter:
    """Sliding window log, bounded per key. Single-process only."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()
        self._last_sweep = time.monotonic()

    def _sweep(self, now: float) -> None:
        """Drop keys with no recent activity so the dict cannot grow forever."""
        if now - self._last_sweep < self._window:
            return
        cutoff = now - self._window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]
        self._last_sweep = now

    async def check(self, key: str) -> RateLimitVerdict:
        now = time.monotonic()
        async with self._lock:
            self._sweep(now)
            cutoff = now - self._window
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self._limit:
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                return RateLimitVerdict(False, 0, retry_after, self._limit)

            hits.append(now)
            return RateLimitVerdict(True, self._limit - len(hits), 0, self._limit)

    async def close(self) -> None:
        self._hits.clear()


class RedisRateLimiter:
    """Shared sliding window using a Redis sorted set, applied atomically."""

    # Trim the window, count, and conditionally add -- in one round trip, so
    # two concurrent requests cannot both observe a pre-increment count.
    _SCRIPT = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
    local count = redis.call('ZCARD', key)
    if count >= limit then
      local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
      return {0, 0, tostring(oldest[2])}
    end
    redis.call('ZADD', key, now, now .. ':' .. math.random())
    redis.call('EXPIRE', key, window + 1)
    return {1, limit - count - 1, '0'}
    """

    def __init__(self, redis_client, limit: int, window_seconds: int) -> None:
        self._redis = redis_client
        self._limit = limit
        self._window = window_seconds
        self._script = redis_client.register_script(self._SCRIPT)

    async def check(self, key: str) -> RateLimitVerdict:
        now = time.time()
        try:
            allowed, remaining, oldest = await self._script(
                keys=[f"cloudleak:rl:{key}"],
                args=[now, self._window, self._limit],
            )
        except Exception:
            # A rate limiter that is down must not take the API down with it.
            # Fail open, but loudly.
            logger.exception("Rate limiter unavailable; allowing request")
            return RateLimitVerdict(True, self._limit, 0, self._limit)

        if int(allowed) == 1:
            return RateLimitVerdict(True, int(remaining), 0, self._limit)

        retry_after = max(1, int(float(oldest) + self._window - now) + 1)
        return RateLimitVerdict(False, 0, retry_after, self._limit)

    async def close(self) -> None:
        await self._redis.aclose()


async def build_rate_limiter(
    limit: int, window_seconds: int, redis_url: Optional[str]
) -> RateLimiter:
    """Return a Redis limiter when configured and reachable, else in-memory."""
    if redis_url:
        try:
            import redis.asyncio as redis  # imported lazily; optional dependency

            client = redis.from_url(redis_url, decode_responses=True)
            await client.ping()
            logger.info("Rate limiting via Redis")
            return RedisRateLimiter(client, limit, window_seconds)
        except ImportError:
            logger.warning("CLOUDLEAK_REDIS_URL set but 'redis' is not installed; using memory")
        except Exception:
            logger.warning("Redis unreachable; falling back to in-memory rate limiting")

    logger.info("Rate limiting in memory (single process only)")
    return InMemoryRateLimiter(limit, window_seconds)
