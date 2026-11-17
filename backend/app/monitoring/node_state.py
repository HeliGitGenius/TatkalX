"""Per-process runtime state shown on /api/health."""
from __future__ import annotations

import os
import time

STARTED_AT = time.time()
PID = os.getpid()
redis_degraded_since: float | None = None


def mark_redis(ok: bool) -> None:
    global redis_degraded_since
    if ok:
        redis_degraded_since = None
    elif redis_degraded_since is None:
        redis_degraded_since = time.time()
