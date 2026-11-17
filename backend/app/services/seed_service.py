"""Simulated railway data and concurrency-safe test-data reset.

ALL trains, schedules and bookings here are SIMULATED. Nothing connects to a real
railway reservation system.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, time, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.distributed.sharding import shard_for_seat

# (number, name, source, destination, dep, arr, duration_min)  -- fictional services
SIM_TRAINS = [
    ("90101", "Konkan Coastal Superfast (SIM)", "Mumbai CSMT", "Madgaon", time(5, 10), time(14, 40), 570),
    ("90102", "Deccan Tatkal Express (SIM)", "Mumbai CSMT", "Pune Jn", time(6, 45), time(10, 5), 200),
    ("90103", "Gateway Capital Mail (SIM)", "Mumbai Central", "New Delhi", time(17, 0), time(8, 35), 935),
    ("90104", "Narmada Valley Express (SIM)", "Mumbai Central", "Ahmedabad Jn", time(13, 30), time(20, 55), 445),
    ("90105", "Sahyadri Night Rider (SIM)", "Lokmanya Tilak T", "Hyderabad Deccan", time(21, 15), time(12, 40), 925),
    ("90106", "Coromandel Link Superfast (SIM)", "Dadar", "Chennai Central", time(20, 0), time(21, 30), 1530),
]
BENCH_TRAIN = ("00001", "TatkalX Benchmark Special (TEST)", "Simulator Origin", "Simulator Destination",
               time(10, 0), time(12, 0), 120)

SLEEPER_BERTHS = ["LB", "MB", "UB", "LB", "MB", "UB", "SL", "SU"]  # 8-berth bay pattern
AC3_BERTHS = SLEEPER_BERTHS
COACHES = [("S1", 72, SLEEPER_BERTHS), ("S2", 72, SLEEPER_BERTHS), ("B1", 64, AC3_BERTHS)]
DAYS_AHEAD = 7
SIM_NAMES = ["Aarav Shah", "Diya Patel", "Kabir Rao", "Meera Iyer", "Rohan Desai", "Ananya Nair",
             "Ishaan Kulkarni", "Saanvi Joshi", "Vihaan Mehta", "Tara Kapoor"]


async def _insert_seats(s: AsyncSession, journey_id: int, coaches, blocked_ratio: float, rng: random.Random) -> list[int]:
    rows = []
    for coach, count, pattern in coaches:
        for n in range(1, count + 1):
            status = "BLOCKED" if rng.random() < blocked_ratio else "AVAILABLE"
            rows.append({"j": journey_id, "c": coach, "n": n, "b": pattern[(n - 1) % len(pattern)],
                         "sh": shard_for_seat(coach, n), "st": status})
    await s.execute(
        text("INSERT INTO journey_seats (journey_id, coach_number, seat_number, berth_type, shard_id, status) "
             "VALUES (:j, :c, :n, :b, :sh, :st)"),
        rows,
    )
    ids = (await s.execute(text("SELECT id FROM journey_seats WHERE journey_id = :j ORDER BY coach_number, seat_number"),
                           {"j": journey_id})).scalars().all()
    return list(ids)


async def _prebook(s: AsyncSession, journey_id: int, ratio: float, rng: random.Random) -> int:
    seats = (await s.execute(text("SELECT id FROM journey_seats WHERE journey_id = :j AND status = 'AVAILABLE'"),
                             {"j": journey_id})).scalars().all()
    chosen = [sid for sid in seats if rng.random() < ratio]
    for sid in chosen:
        bid = uuid.uuid4()
        # Same pattern as live bookings: claim seat, then insert booking (deferred FK).
        await s.execute(text("UPDATE journey_seats SET status='BOOKED', booking_id=:b WHERE id=:id AND status='AVAILABLE'"),
                        {"b": bid, "id": sid})
        await s.execute(
            text("INSERT INTO bookings (id, pnr, user_id, journey_id, journey_seat_id, passenger_name, passenger_age, "
                 "passenger_gender, status, node_id) VALUES (:id, :pnr, :u, :j, :sid, :n, :a, :g, 'CONFIRMED', 'SEED')"),
            {"id": bid, "pnr": str(rng.randint(1_000_000_000, 9_999_999_999)), "u": f"sim-user-{rng.randint(1, 400)}",
             "j": journey_id, "sid": sid, "n": rng.choice(SIM_NAMES), "a": rng.randint(18, 75), "g": rng.choice("MF")},
        )
    return len(chosen)


async def seed(s: AsyncSession, force: bool = False) -> dict:
    """Idempotent seed of simulated trains and the next DAYS_AHEAD days of journeys."""
    await s.execute(text("SELECT pg_advisory_xact_lock(424201)"))  # one seeder at a time
    existing = (await s.execute(text("SELECT count(*) FROM journeys WHERE is_test = false"))).scalar_one()
    if existing and not force:
        return {"seeded": False, "reason": "data already present", "journeys": existing}
    if force:
        await s.execute(text("TRUNCATE booking_events, booking_requests, bookings, journey_seats, journeys, trains, "
                             "benchmark_runs, naive_demo_bookings, naive_demo_seats RESTART IDENTITY CASCADE"))
    rng = random.Random(2026)
    today = date.today()
    journeys = prebooked = 0
    for tn in SIM_TRAINS + [BENCH_TRAIN]:
        tid = (await s.execute(
            text("INSERT INTO trains (train_number, train_name, source, destination, departure_time, arrival_time, "
                 "duration_minutes) VALUES (:a,:b,:c,:d,:e,:f,:g) ON CONFLICT (train_number) DO UPDATE SET "
                 "train_name = EXCLUDED.train_name RETURNING id"),
            dict(zip("abcdefg", tn)))).scalar_one()
        if tn is BENCH_TRAIN:
            continue
        for d in range(DAYS_AHEAD):
            jid = (await s.execute(text("INSERT INTO journeys (train_id, journey_date) VALUES (:t, :d) RETURNING id"),
                                   {"t": tid, "d": today + timedelta(days=d)})).scalar_one()
            await _insert_seats(s, jid, COACHES, blocked_ratio=0.03, rng=rng)
            prebooked += await _prebook(s, jid, ratio=0.15 + 0.1 * d / DAYS_AHEAD + rng.random() * 0.2, rng=rng)
            journeys += 1
    return {"seeded": True, "journeys": journeys, "prebooked": prebooked}


async def reset_test_journey(s: AsyncSession, seats: int, label: str) -> dict:
    """Concurrency-safe reset for benchmarks.

    * pg_advisory_xact_lock serialises concurrent resets (two dashboards clicking at once).
    * Old TEST journeys (and everything hanging off them) are deleted.
    * A brand-new journey id is created, so no state from a previous run can leak into this one.
    """
    await s.execute(text("SELECT pg_advisory_xact_lock(424202)"))
    old = (await s.execute(text("SELECT id FROM journeys WHERE is_test = true"))).scalars().all()
    if old:
        await s.execute(text("DELETE FROM booking_requests WHERE booking_id IN (SELECT id FROM bookings WHERE journey_id = ANY(:ids))"), {"ids": old})
        await s.execute(text("DELETE FROM journeys WHERE id = ANY(:ids)"), {"ids": old})
    tid = (await s.execute(text("SELECT id FROM trains WHERE train_number = :n"), {"n": BENCH_TRAIN[0]})).scalar_one_or_none()
    if tid is None:
        tid = (await s.execute(
            text("INSERT INTO trains (train_number, train_name, source, destination, departure_time, arrival_time, "
                 "duration_minutes) VALUES (:a,:b,:c,:d,:e,:f,:g) RETURNING id"), dict(zip("abcdefg", BENCH_TRAIN)))).scalar_one()
    jid = (await s.execute(
        text("INSERT INTO journeys (train_id, journey_date, is_test, label) VALUES (:t, :d, true, :l) RETURNING id"),
        {"t": tid, "d": date.today() + timedelta(days=1), "l": label})).scalar_one()
    coaches, remaining, i = [], seats, 1
    while remaining > 0:
        n = min(72, remaining)
        coaches.append((f"T{i}", n, SLEEPER_BERTHS))
        remaining -= n
        i += 1
    seat_ids = await _insert_seats(s, jid, coaches, blocked_ratio=0.0, rng=random.Random(0))
    return {"journey_id": jid, "seat_ids": seat_ids, "seats": len(seat_ids), "deleted_old_test_journeys": len(old)}


async def reset_naive_demo(s: AsyncSession) -> int:
    await s.execute(text("SELECT pg_advisory_xact_lock(424203)"))
    await s.execute(text("TRUNCATE naive_demo_bookings, naive_demo_seats RESTART IDENTITY"))
    return (await s.execute(text("INSERT INTO naive_demo_seats (label) VALUES ('NAIVE-1') RETURNING id"))).scalar_one()
