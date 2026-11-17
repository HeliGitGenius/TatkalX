"""Correctness tests: concurrency, idempotency, constraints, locks, rate limiting, Redis outage."""
import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import get_sessionmaker
from app.core.redis_client import close_redis, get_redis
from app.distributed.lock import DistributedLock

from .conftest import booking_body, make_journey


def headers(user: str, key: str | None = None) -> dict:
    return {"X-User-Id": user, "Idempotency-Key": key or f"k-{uuid.uuid4().hex}"}


async def db_scalar(sql: str, **params):
    async with get_sessionmaker()() as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def fire(client, n, body_fn, user_fn=lambda i: f"user-{i}-{uuid.uuid4().hex[:6]}"):
    start = asyncio.Event()

    async def one(i):
        await start.wait()
        return await client.post("/api/bookings", json=body_fn(i), headers=headers(user_fn(i)))

    tasks = [asyncio.create_task(one(i)) for i in range(n)]
    await asyncio.sleep(0.05)
    start.set()
    return await asyncio.gather(*tasks)


# ----------------------------------------------------------------------------- concurrency
@pytest.mark.parametrize("mode", ["redis_lock", "db_only"])
async def test_one_seat_many_concurrent_requests_exactly_one_wins(client, settings, mode):
    settings.coordination_mode = mode
    journey_id, (seat_id,) = await make_journey(1)
    responses = await fire(client, 150, lambda i: booking_body(journey_id, seat_id))
    codes = [r.status_code for r in responses]
    assert codes.count(201) == 1, codes
    assert set(codes) <= {201, 409}, set(codes)
    conflicts = {r.json()["error"]["code"] for r in responses if r.status_code == 409}
    assert conflicts <= {"SEAT_UNAVAILABLE", "SEAT_BUSY"}
    assert await db_scalar("SELECT count(*) FROM bookings WHERE journey_id=:j AND status='CONFIRMED'", j=journey_id) == 1
    assert await db_scalar("SELECT status FROM journey_seats WHERE id=:s", s=seat_id) == "BOOKED"


async def test_limited_inventory_auto_allocation_never_oversells(client):
    journey_id, seat_ids = await make_journey(25)
    responses = await fire(client, 200, lambda i: booking_body(journey_id))
    codes = [r.status_code for r in responses]
    assert codes.count(201) == 25, codes
    assert all(r.json()["error"]["code"] == "SOLD_OUT" for r in responses if r.status_code == 409)
    seats = [r.json()["seat"]["id"] for r in responses if r.status_code == 201]
    assert len(set(seats)) == 25 and set(seats) == set(seat_ids)
    assert await db_scalar("SELECT count(*) FROM journey_seats WHERE journey_id=:j AND status='BOOKED'", j=journey_id) == 25


async def test_limited_inventory_targeted_seats(client):
    journey_id, seat_ids = await make_journey(10)
    responses = await fire(client, 120, lambda i: booking_body(journey_id, seat_ids[i % 10]))
    assert [r.status_code for r in responses].count(201) == 10
    booked = [r.json()["seat"]["id"] for r in responses if r.status_code == 201]
    assert sorted(booked) == sorted(seat_ids)


# ----------------------------------------------------------------------------- idempotency
async def test_retry_with_same_key_returns_original_booking(client):
    journey_id, seat_ids = await make_journey(2)
    h = headers("retry-user", "retry-key-0001")
    first = await client.post("/api/bookings", json=booking_body(journey_id, seat_ids[0]), headers=h)
    second = await client.post("/api/bookings", json=booking_body(journey_id, seat_ids[0]), headers=h)
    assert first.status_code == second.status_code == 201
    assert first.json()["booking_id"] == second.json()["booking_id"]
    assert second.headers.get("idempotent-replayed") == "true"
    assert await db_scalar("SELECT count(*) FROM bookings WHERE journey_id=:j", j=journey_id) == 1


async def test_concurrent_requests_with_same_key_create_one_booking(client):
    journey_id, _ = await make_journey(5)
    h = headers("same-key-user", "same-key-concurrent-01")
    body = booking_body(journey_id)
    responses = await asyncio.gather(*[client.post("/api/bookings", json=body, headers=h) for _ in range(15)])
    codes = [r.status_code for r in responses]
    assert set(codes) <= {201, 409}
    ids = {r.json()["booking_id"] for r in responses if r.status_code == 201}
    assert len(ids) == 1
    assert await db_scalar("SELECT count(*) FROM bookings WHERE journey_id=:j", j=journey_id) == 1
    # after the dust settles, a retry replays the single original booking
    again = await client.post("/api/bookings", json=body, headers=h)
    assert again.status_code == 201 and again.json()["booking_id"] in ids


async def test_conflict_outcome_is_also_replayed(client):
    journey_id, (seat_id,) = await make_journey(1)
    await client.post("/api/bookings", json=booking_body(journey_id, seat_id), headers=headers("winner"))
    h = headers("loser", "loser-key-000001")
    a = await client.post("/api/bookings", json=booking_body(journey_id, seat_id), headers=h)
    b = await client.post("/api/bookings", json=booking_body(journey_id, seat_id), headers=h)
    assert a.status_code == b.status_code == 409
    assert a.json() == b.json()


async def test_same_key_different_payload_rejected(client):
    journey_id, seat_ids = await make_journey(2)
    h = headers("reuse-user", "reuse-key-000001")
    assert (await client.post("/api/bookings", json=booking_body(journey_id, seat_ids[0]), headers=h)).status_code == 201
    r = await client.post("/api/bookings", json=booking_body(journey_id, seat_ids[1]), headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_missing_idempotency_key_and_bad_payload(client):
    journey_id, seat_ids = await make_journey(1)
    r = await client.post("/api/bookings", json=booking_body(journey_id, seat_ids[0]), headers={"X-User-Id": "u-123"})
    assert r.status_code == 422
    bad = {"journey_id": journey_id, "seat_id": seat_ids[0], "auto_allocate": True,
           "passenger": {"name": "<script>", "age": 0, "gender": "Q"}}
    r = await client.post("/api/bookings", json=bad, headers=headers("u-123"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


# ----------------------------------------------------------------------------- database invariants
async def test_partial_unique_index_blocks_second_confirmed_booking(client):
    """Even if application logic were bypassed, PostgreSQL refuses a 2nd CONFIRMED booking for a seat."""
    journey_id, (seat_id,) = await make_journey(1)
    r = await client.post("/api/bookings", json=booking_body(journey_id, seat_id), headers=headers("first"))
    assert r.status_code == 201
    with pytest.raises(IntegrityError):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text(
                "INSERT INTO bookings (id, pnr, user_id, journey_id, journey_seat_id, passenger_name, passenger_age, "
                "passenger_gender, node_id) VALUES (:id, '9999999999', 'intruder', :j, :s, 'Bypass', 30, 'M', 'X')"),
                {"id": uuid.uuid4(), "j": journey_id, "s": seat_id})


async def test_check_constraint_keeps_seat_state_and_owner_in_sync():
    journey_id, (seat_id,) = await make_journey(1)
    with pytest.raises(IntegrityError):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text("UPDATE journey_seats SET status='BOOKED' WHERE id=:s"), {"s": seat_id})


# ----------------------------------------------------------------------------- distributed lock
async def test_lock_is_exclusive_and_release_is_owner_safe():
    lock = DistributedLock(ttl_ms=150, wait_ms=0, retry_interval_ms=5)
    key = f"tatkalx:test:lock:{uuid.uuid4().hex}"
    a = await lock.acquire(key)
    assert a is not None
    assert await lock.acquire(key) is None          # mutual exclusion
    await asyncio.sleep(0.25)                       # A's lease expires (simulated pause)
    b = await lock.acquire(key)
    assert b is not None
    assert await lock.release(a) is False           # stale owner must NOT delete B's lock
    assert await get_redis().get(key) == b.token
    assert await lock.release(b) is True
    assert await get_redis().get(key) is None


async def test_lock_waits_then_times_out():
    lock = DistributedLock(ttl_ms=2000, wait_ms=100, retry_interval_ms=10)
    key = f"tatkalx:test:lock:{uuid.uuid4().hex}"
    held = await lock.acquire(key)
    t0 = asyncio.get_running_loop().time()
    assert await lock.acquire(key) is None
    assert asyncio.get_running_loop().time() - t0 >= 0.09
    await lock.release(held)


# ----------------------------------------------------------------------------- rate limiting
async def test_per_user_rate_limit_returns_429(client, settings):
    settings.rate_limit_per_user = 5
    journey_id, _ = await make_journey(50)
    user = f"spammer-{uuid.uuid4().hex[:6]}"
    rs = [await client.post("/api/bookings", json=booking_body(journey_id), headers=headers(user)) for _ in range(9)]
    codes = [r.status_code for r in rs]
    assert codes.count(201) == 5 and codes.count(429) == 4
    assert rs[-1].json()["error"]["code"] == "RATE_LIMITED" and "retry-after" in rs[-1].headers


# ----------------------------------------------------------------------------- Redis outage
async def test_redis_down_fail_open_still_correct(client, settings):
    settings.redis_url = "redis://127.0.0.1:6390/0"  # nothing listens here
    settings.redis_socket_timeout_s = 0.2
    await close_redis()
    try:
        journey_id, (seat_id,) = await make_journey(1)
        responses = await fire(client, 40, lambda i: booking_body(journey_id, seat_id))
        assert [r.status_code for r in responses].count(201) == 1
        health = await client.get("/api/health")
        assert health.json()["status"] == "degraded"
        ok = [r for r in responses if r.status_code == 201][0]
        assert ok.json()["coordination"] == "db_only (redis down)"
    finally:
        await close_redis()
