"""Shared Redis client with short timeouts so a Redis outage is detected quickly."""
from __future__ import annotations

import redis.asyncio as redis

from app.core.config import get_settings

_client: redis.Redis | None = None


class RedisUnavailable(Exception):
    """Raised when Redis cannot be reached within the configured timeout."""


REDIS_ERRORS = (redis.ConnectionError, redis.TimeoutError, OSError)


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        s = get_settings()
        _client = redis.from_url(
            s.redis_url,
            socket_timeout=s.redis_socket_timeout_s,
            socket_connect_timeout=s.redis_socket_timeout_s,
            decode_responses=True,
            max_connections=200,
            retry_on_timeout=False,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
