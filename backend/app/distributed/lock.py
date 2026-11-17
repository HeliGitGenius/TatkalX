"""Redis-based distributed lock (single Redis instance, lease-based).

Safety properties implemented here:
  * Atomic acquisition:   SET key <token> NX PX <ttl>   (one round trip, no check-then-set gap)
  * Unique owner token:   uuid4 per acquisition
  * Finite lease:         PX ttl, so a crashed owner cannot hold the seat forever
  * Safe release:         Lua compare-and-delete; we delete ONLY if the stored token is ours
  * Bounded waiting:      acquisition gives up after `wait_ms` (no unbounded queueing)
  * Explicit failure:     Redis errors raise RedisUnavailable; the caller decides the policy

What this lock does NOT guarantee (and why PostgreSQL stays authoritative):
  * If the owner pauses (GC, VM stall) longer than the TTL, the lease expires and a second
    process can acquire the "same" lock while the first still believes it holds it.
  * Redis replication is asynchronous; a failover can lose a just-granted lock.
  * Redlock (N independent Redis masters) narrows but does not remove these windows because
    it still relies on bounded clock drift and bounded pauses.
Therefore the lock is an optimisation that reduces wasted DB work and contention; the
conditional UPDATE + unique index in PostgreSQL is what actually prevents double booking.
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass

from app.core.redis_client import REDIS_ERRORS, RedisUnavailable, get_redis

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""


@dataclass(frozen=True)
class LockHandle:
    key: str
    token: str
    acquired_at: float
    ttl_ms: int
    wait_ms: float


class DistributedLock:
    def __init__(self, ttl_ms: int, wait_ms: int, retry_interval_ms: int):
        self.ttl_ms = ttl_ms
        self.wait_ms = wait_ms
        self.retry_interval_ms = retry_interval_ms
        self._release_script = None

    async def acquire(self, key: str) -> LockHandle | None:
        """Return a LockHandle, or None if the lock is still held by someone else after wait_ms."""
        r = get_redis()
        token = uuid.uuid4().hex
        start = time.perf_counter()
        deadline = start + self.wait_ms / 1000
        try:
            while True:
                if await r.set(key, token, nx=True, px=self.ttl_ms):
                    now = time.perf_counter()
                    return LockHandle(key, token, now, self.ttl_ms, (now - start) * 1000)
                if time.perf_counter() >= deadline:
                    return None
                # jittered back-off avoids a thundering herd of retries
                await asyncio.sleep(self.retry_interval_ms / 1000 * (0.5 + random.random()))
        except REDIS_ERRORS as exc:
            raise RedisUnavailable(str(exc)) from exc

    async def release(self, handle: LockHandle) -> bool:
        """Release only if we still own the lock. Returns False if the lease already expired
        or was taken by another owner (never deletes someone else's lock)."""
        r = get_redis()
        try:
            if self._release_script is None:
                self._release_script = r.register_script(_RELEASE_LUA)
            return bool(await self._release_script(keys=[handle.key], args=[handle.token]))
        except REDIS_ERRORS as exc:
            raise RedisUnavailable(str(exc)) from exc

    @staticmethod
    def lease_remaining_ms(handle: LockHandle) -> float:
        return handle.ttl_ms - (time.perf_counter() - handle.acquired_at) * 1000
