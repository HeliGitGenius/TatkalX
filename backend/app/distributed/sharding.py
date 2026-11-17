"""Logical seat sharding.

Every seat is mapped to one of NUM_SHARDS logical shards with a stable hash of its
(coach, seat) identity. The shard id is stored on the seat row and used to:
  * namespace Redis lock keys with a hash tag  -> tatkalx:{shard-1}:lock:...
    (in a Redis Cluster all keys of one shard land in the same hash slot/node)
  * report per-shard load in the admin dashboard
  * select the database for that shard via SHARD_DSN_MAP (today every shard points to the
    same PostgreSQL database -- this is a SIMULATION, not three replicated clusters).

Requests for the same seat always hash to the same shard and the same lock key, so they
are coordinated no matter which backend node (A/B/C) receives them.
"""
from __future__ import annotations

import zlib

from app.core.config import get_settings


def shard_for_seat(coach_number: str, seat_number: int, num_shards: int | None = None) -> int:
    n = num_shards or get_settings().num_shards
    return zlib.crc32(f"{coach_number}-{seat_number}".encode()) % n + 1  # shards are 1-based


def seat_lock_key(shard_id: int, journey_id: int, seat_id: int) -> str:
    return f"tatkalx:{{shard-{shard_id}}}:lock:journey:{journey_id}:seat:{seat_id}"


def shard_home_node(shard_id: int) -> str:
    """Informational 'preferred owner' node for a shard (A, B, C). Any node may serve any shard
    because the authoritative state is shared; this is what a shard-aware router would use."""
    return "ABC"[(shard_id - 1) % 3]


def shard_dsn(shard_id: int) -> str:
    """Extension point for physical sharding: map shard -> database URL."""
    return get_settings().database_url
