"""Redis fixed-window rate limiter (atomic INCR + PEXPIRE in one Lua call).

Two limits are applied to booking endpoints:
  * per user   (X-User-Id)         -- always enforced
  * per client IP (X-Real-IP from Nginx) -- ceiling raised in RATE_LIMIT_TEST_MODE so a
    benchmark that sends 500 requests from one machine is not throttled as a single client.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.redis_client import REDIS_ERRORS, RedisUnavailable, get_redis

_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return {current, redis.call('PTTL', KEYS[1])}
"""


@dataclass
class RateDecision:
    allowed: bool
    scope: str
    limit: int
    current: int
    retry_after_s: int


class RateLimiter:
    def __init__(self) -> None:
        self._script = None

    async def _hit(self, key: str, window_ms: int) -> tuple[int, int]:
        r = get_redis()
        if self._script is None:
            self._script = r.register_script(_LUA)
        current, pttl = await self._script(keys=[key], args=[window_ms])
        return int(current), int(pttl)

    async def check(self, user_id: str, client_ip: str) -> RateDecision:
        s = get_settings()
        window = s.rate_limit_window_s
        bucket = int(time.time() // window)
        try:
            checks = [
                ("user", f"tatkalx:rl:user:{user_id}:{bucket}", s.rate_limit_per_user),
                ("ip", f"tatkalx:rl:ip:{client_ip}:{bucket}", s.effective_ip_limit),
            ]
            for scope, key, limit in checks:
                current, pttl = await self._hit(key, window * 1000)
                if current > limit:
                    return RateDecision(False, scope, limit, current, max(1, pttl // 1000))
            return RateDecision(True, "ok", 0, 0, 0)
        except REDIS_ERRORS as exc:
            raise RedisUnavailable(str(exc)) from exc
