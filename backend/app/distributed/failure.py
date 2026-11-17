"""Development-only failure injection.

* Injected delay is cluster-wide (stored in Redis) so every node widens its critical section.
* 'Unhealthy' (brown-out) is node-local: the node answers 503 so Nginx fails over.
* 'Crash' is node-local: the process exits with code 75 after replying; the supervisor
  (scripts/cluster.py, or Docker's restart policy) brings it back after a delay.
"""
from __future__ import annotations

import time

from app.core.redis_client import REDIS_ERRORS, get_redis

DELAY_KEY = "tatkalx:failure:delay_ms"
_unhealthy_until = 0.0
_delay_cache: tuple[float, int] = (0.0, 0)


def set_unhealthy(seconds: float) -> None:
    global _unhealthy_until
    _unhealthy_until = time.time() + seconds


def clear_unhealthy() -> None:
    global _unhealthy_until
    _unhealthy_until = 0.0


def unhealthy_remaining() -> float:
    return max(0.0, _unhealthy_until - time.time())


async def set_delay(delay_ms: int, ttl_s: int) -> None:
    await get_redis().set(DELAY_KEY, delay_ms, ex=ttl_s)


async def clear_delay() -> None:
    await get_redis().delete(DELAY_KEY)


async def current_delay_ms() -> int:
    """Read the injected delay, cached for 1s so the hot path does not hit Redis every time."""
    global _delay_cache
    ts, val = _delay_cache
    if time.time() - ts < 1.0:
        return val
    try:
        raw = await get_redis().get(DELAY_KEY)
        val = int(raw) if raw else 0
    except REDIS_ERRORS:
        val = 0
    _delay_cache = (time.time(), val)
    return val
