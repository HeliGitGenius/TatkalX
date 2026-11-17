# TatkalX — Measured results

All numbers below come from real runs on **15 Oct 2026** (raw per-request JSON/CSV in `load_testing/reports/`).
Nothing is hard-coded: each figure is computed from the HTTP responses and then re-checked with SQL against PostgreSQL.

**Test environment.** One cloud VM with 2 vCPUs and 8 GB RAM running *everything* at once: PostgreSQL 16, Redis 7,
Nginx, three FastAPI nodes (Python 3.12, one uvicorn process each) and the load generator itself. Docker Hub was not
reachable from that VM, so the stack ran natively (same topology as `docker-compose.yml`). Latency is therefore
dominated by CPU sharing; on separate machines it would be far lower. Correctness results do not depend on this.

## Concurrency benchmarks (500 simultaneous requests through Nginx)

| # | Experiment | Mode | Berths | Booked | 409 taken | Dup. | 429 | Errors | Sold twice | A / B / C | Median / p95 ms | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | One seat | redis_lock | 1 | **1** | 499 | 0 | 0 | 0 | **0** | 167/167/166 | 1894 / 2611 | ✅ consistent |
| 2 | Limited, targeted seats | redis_lock | 50 | **50** | 450 | 0 | 0 | 0 | **0** | 167/166/167 | 2008 / 2696 | ✅ |
| 3 | Limited, auto-allocation (SKIP LOCKED) | redis_lock | 50 | **50** | 450 | 0 | 0 | 0 | **0** | 167/167/166 | 2150 / 2775 | ✅ |
| 4 | Retry storm: 100 bookings × 5 copies, same key | redis_lock | 100 | **100** | 0 | 400 | 0 | 0 | **0** | 166/167/167 | 1633 / 2204 | ✅ |
| 5 | One user, 60 requests (limit 20/min) | redis_lock | 60 | **20** | 0 | 0 | **40** | 0 | 0 | 19/20/21 | 140 / 221 | ✅ |
| 6 | **Unprotected check-then-write (demo table)** | — | 1 | **4** | 496 | 0 | 0 | 0 | **3** | 167/166/167 | 962 / 1358 | ❌ double booking (expected) |
| 7 | One seat + 300 ms injected delay | redis_lock | 1 | **1** | 499 | 0 | 0 | 0 | **0** | 167/166/167 | 2034 / 2838 | ✅ |
| 8 | Limited + 300 ms injected delay | redis_lock | 50 | **50** | 450 | 0 | 0 | 0 | **0** | 167/167/166 | 2164 / 2787 | ✅ |
| 9 | One seat | db_only | 1 | **1** | 499 | 0 | 0 | 0 | **0** | 167/167/166 | 2101 / 2891 | ✅ |
| 10 | Limited, targeted | db_only | 50 | **50** | 450 | 0 | 0 | 0 | **0** | 166/167/167 | 1939 / 2646 | ✅ |
| 11 | Limited, auto | db_only | 50 | **50** | 450 | 0 | 0 | 0 | **0** | 167/166/167 | 2078 / 2861 | ✅ |
| 12 | Limited, rate-limit **test mode off** | redis_lock | 50 | **50** | 70 | 0 | **380** | 0 | **0** | 167/167/166 | 1690 / 1936 | ✅ |

Each burst took 2.1–3.1 s end to end (≈160–240 requests/s on the shared 2-vCPU VM).

What each row shows:
- **1, 9** — 500 requests for one berth, spread evenly across the three nodes: exactly one booking, with or without Redis.
- **2, 3, 10, 11** — never more bookings than berths, every berth used once; both targeted and automatic allocation.
- **4** — of the 400 duplicates, 97 were answered from the stored result and 303 were told the original was still in flight (409 `REQUEST_IN_PROGRESS`); a later retry pass of all 100 keys returned the *same* booking id 100/100 times.
- **5** — the atomic Redis counter let exactly 20 through.
- **6** — the same load against a deliberately unprotected check-then-write endpoint sold one berth four times. This is the bug the real path prevents.
- **7, 8** — with a 300 ms pause inside every booking transaction, Redis granted **102 locks for 50 berths** in run 8 (locks expire/release and later holders find the seat gone), 119 requests gave up waiting (28 told `SEAT_BUSY`), and still exactly 50 bookings committed: **the database, not the lock, decided.**
- **12** — without test mode, the per-IP ceiling (120/min) turned 380 requests into 429s; protections stayed on and the result stayed correct. This is why the benchmark uses test mode (which only lifts the per-IP ceiling).

PostgreSQL checks run after each burst (all pass for runs 1–5, 7–12): no seat has two CONFIRMED bookings; bookings ≤ seats; every BOOKED seat points to its CONFIRMED booking and vice versa; the set of booking ids returned to clients equals the set in PostgreSQL; zero unexpected errors.

## Failure injection (`load_testing/failure_demo.py`)

480 bookings at 15/s for 32 s, with controlled client retries (max 2, same key):

| t | Injected | Observed |
|---|---|---|
| 4 s | Node A process killed (exit 75) | A's share went to B and C within the same second; A was down ~8 s, restarted by the supervisor with a new pid, healthy at 13 s and taking traffic again by 15 s |
| 16 s | Node B browned out (503 for 4 s) | Nginx skipped B (`http_503` failover); A and C carried the traffic |
| 22 s | Redis stopped for 5 s | All nodes reported `degraded`, kept booking in DB-only mode; recovered automatically when Redis returned |

Result: **480 / 480 booked, 0 failed**, 4 requests transparently re-sent by Nginx to another node, 0 needed a client retry,
no seat booked twice, no request produced two bookings, client-visible bookings = PostgreSQL bookings.

**Database failure** (checked by hand): with PostgreSQL stopped, a booking returned `503 SERVICE_UNAVAILABLE`
("safe to retry with the same Idempotency-Key") and `/api/health` reported `unhealthy`. After restarting PostgreSQL the same
request (same key) returned `201`, and a further retry returned the same booking with `Idempotent-Replayed: true`.

## Automated tests

`cd backend && pytest` — **15 passed** (run 3× in a row, all green), against real PostgreSQL and Redis:

- one seat, 150 concurrent requests → exactly one winner (`redis_lock` and `db_only`)
- 200 auto-allocated requests for 25 seats → exactly 25, all distinct
- 120 targeted requests for 10 seats → exactly 10
- retry with same key returns the original booking; 15 concurrent same-key requests → 1 booking; conflict outcomes are replayed too
- same key + different payload → 422; missing key / invalid payload → 422
- partial unique index rejects a second CONFIRMED booking inserted with raw SQL; CHECK constraint rejects BOOKED without booking_id
- lock is exclusive; a stale owner cannot delete the new owner's lock; bounded wait times out
- per-user rate limit → 429 with Retry-After
- Redis unreachable → bookings still correct (`db_only (redis down)`), health `degraded`

## Not tested here (disclosed)

- `docker compose up` could not be executed (Docker Hub blocked in the build environment). `docker compose config` validates the file, and the same topology was run natively with identical configuration values.
- Throughput on separate machines, multiple Nginx/PostgreSQL/Redis instances, and network partitions between nodes.
