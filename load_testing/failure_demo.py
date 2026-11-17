"""Failure-injection demonstration (DEVELOPMENT ONLY).

Sends a steady stream of bookings through the load balancer while it:
  t= 4s  crashes Node A (process exits; supervisor / Docker restart policy revives it later)
  t=16s  browns-out Node B for 4 s (it answers 503 -> Nginx fails over)
  t=22s  stops Redis for 5 s (bookings continue in fail_open DB-only mode)  [--redis-stop/--redis-start]
and records, second by second, which nodes served traffic and what failed.

Clients use CONTROLLED retries: at most 2 retries, 0.5 s back-off, SAME Idempotency-Key,
only for 503 / connection errors. Afterwards every invariant is verified in PostgreSQL.

    python load_testing/failure_demo.py --duration 32 --rate 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx

from benchmark import _pg_dsn, verify_in_postgres  # type: ignore

REPORTS = Path(__file__).resolve().parent / "reports"


async def booking_with_retries(client, journey_id, i, uid, t_start, log):
    headers = {"X-User-Id": f"fd-{uid}-u{i}", "Idempotency-Key": f"fd-{uid}-k{i}"}
    body = {"journey_id": journey_id, "auto_allocate": True,
            "passenger": {"name": f"Failover Passenger {chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}", "age": 20 + i % 50, "gender": "MF"[i % 2]}}
    attempts = []
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            r = await client.post("/api/bookings", json=body, headers=headers)
            code = (r.json().get("error") or {}).get("code") if r.headers.get("content-type", "").startswith("application/json") else None
            attempts.append({"status": r.status_code, "node": r.headers.get("x-served-by"),
                             "upstream": r.headers.get("x-upstream-addr"), "code": code,
                             "replayed": r.headers.get("idempotent-replayed") == "true",
                             "booking_id": r.json().get("booking_id") if r.status_code in (200, 201) else None})
        except Exception as exc:
            attempts.append({"status": None, "node": None, "error": type(exc).__name__})
        attempts[-1]["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        st = attempts[-1]["status"]
        if st is not None and st not in (503, 502, 504) and not (st == 409 and attempts[-1].get("code") == "REQUEST_IN_PROGRESS"):
            break
        await asyncio.sleep(0.5)
    final = attempts[-1]
    log.append({"i": i, "t": round(time.perf_counter() - t_start, 2), "attempts": len(attempts), "final": final,
                "first": attempts[0], "key": headers["Idempotency-Key"]})


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("LOAD_BALANCER_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", "change-me-admin-token"))
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--duration", type=float, default=32)
    ap.add_argument("--rate", type=float, default=20, help="bookings per second")
    ap.add_argument("--redis-stop", default="redis-cli -p 6379 shutdown nosave")
    ap.add_argument("--redis-start", default="redis-server --port 6379 --bind 127.0.0.1 --daemonize yes --save '' --appendonly no")
    ap.add_argument("--skip-redis", action="store_true")
    a = ap.parse_args()
    admin = {"X-Admin-Token": a.admin_token}
    uid = uuid.uuid4().hex[:4]
    timeline_events: list[dict] = []
    health_samples: list[dict] = []
    log: list[dict] = []

    async with httpx.AsyncClient(base_url=a.base_url, timeout=httpx.Timeout(15, connect=3)) as client:
        total = int(a.duration * a.rate)
        setup = (await client.post("/api/admin/reset-test-data", headers=admin,
                                   json={"scenario": "limited", "seats": min(500, total + 20)})).json()
        journey_id = setup["journey_id"]
        t_start = time.perf_counter()

        def mark(what: str):
            timeline_events.append({"t": round(time.perf_counter() - t_start, 2), "event": what})
            print(f"[t={timeline_events[-1]['t']:5.1f}s] {what}", flush=True)

        async def chaos():
            await asyncio.sleep(4)
            try:
                r = await client.post("/node/a/api/admin/failure/crash", headers=admin)
                mark(f"CRASH Node A injected -> {r.json().get('action')}")
            except Exception as exc:
                mark(f"CRASH Node A request ended: {type(exc).__name__}")
            await asyncio.sleep(12)
            r = await client.post("/node/b/api/admin/failure/unhealthy", headers=admin, json={"seconds": 4})
            mark(f"BROWN-OUT Node B for 4s (status {r.status_code})")
            if not a.skip_redis:
                await asyncio.sleep(6)
                subprocess.run(shlex.split(a.redis_stop), capture_output=True)
                mark("REDIS STOPPED (process failure of the coordination service)")
                await asyncio.sleep(5)
                subprocess.run(shlex.split(a.redis_start), capture_output=True)
                mark("REDIS RESTARTED")

        async def watch_health():
            while time.perf_counter() - t_start < a.duration + 2:
                sample = {"t": round(time.perf_counter() - t_start, 1)}
                for n in "abc":
                    try:
                        h = (await client.get(f"/node/{n}/api/health", timeout=0.8)).json()
                        sample[n.upper()] = h.get("status", "?") if "status" in h else "unhealthy"
                        sample[f"{n.upper()}_pid"] = h.get("pid")
                    except Exception:
                        sample[n.upper()] = "down"
                health_samples.append(sample)
                await asyncio.sleep(1)

        async def traffic():
            tasks = []
            for i in range(total):
                target = t_start + i / a.rate
                await asyncio.sleep(max(0, target - time.perf_counter()))
                tasks.append(asyncio.create_task(booking_with_retries(client, journey_id, i, uid, t_start, log)))
            await asyncio.gather(*tasks)

        mark(f"traffic start: {total} bookings at {a.rate}/s, journey {journey_id}")
        await asyncio.gather(traffic(), chaos(), watch_health())
        mark("traffic finished")
        await asyncio.sleep(10)  # give Node A time to be restarted if still down
        final_health = {}
        for n in "abc":
            try:
                final_health[n.upper()] = (await client.get(f"/node/{n}/api/health")).json()
            except Exception as exc:
                final_health[n.upper()] = {"status": "down", "error": type(exc).__name__}

    # ---------------------------------------------------------------- analysis
    per_second: dict[int, Counter] = defaultdict(Counter)
    for e in log:
        sec = int(e["t"])
        f = e["final"]
        per_second[sec][f"node_{f.get('node') or 'none'}"] += 1
        if f.get("status") == 201:
            per_second[sec]["success"] += 1
        elif f.get("status") == 409:
            per_second[sec]["conflict"] += 1
        else:
            per_second[sec]["failed"] += 1
        if e["attempts"] > 1:
            per_second[sec]["client_retries"] += e["attempts"] - 1
        if f.get("upstream") and "," in f["upstream"]:
            per_second[sec]["lb_failovers"] += 1
    timeline = [{"second": s, **{k: v for k, v in sorted(c.items())}} for s, c in sorted(per_second.items())]

    finals = Counter((e["final"].get("status") or "conn_error") for e in log)
    success_ids = {e["final"]["booking_id"] for e in log if e["final"].get("booking_id")}
    db = await verify_in_postgres(_pg_dsn(a.database_url), journey_id, success_ids)
    conn = await asyncpg.connect(_pg_dsn(a.database_url))
    dup_keys = await conn.fetchval(
        "SELECT count(*) FROM (SELECT user_id FROM bookings WHERE journey_id=$1 GROUP BY user_id HAVING count(*)>1) x",
        journey_id)
    await conn.close()
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "journey_id": journey_id,
        "requests": len(log),
        "final_status_counts": {str(k): v for k, v in finals.items()},
        "requests_needing_client_retry": sum(1 for e in log if e["attempts"] > 1),
        "nginx_transparent_failovers": sum(1 for e in log if e["final"].get("upstream") and "," in e["final"]["upstream"]),
        "served_by_node": dict(Counter(e["final"].get("node") or "none" for e in log)),
        "events": timeline_events,
        "timeline": timeline,
        "health_samples": health_samples,
        "node_a_restarted": bool(final_health.get("A", {}).get("pid")) and final_health["A"].get("pid") != next(
            (h.get("A_pid") for h in health_samples if h.get("A_pid")), None),
        "final_health": {k: {"status": v.get("status"), "pid": v.get("pid"), "uptime_s": v.get("uptime_s"),
                             "database": v.get("database"), "redis": v.get("redis")} for k, v in final_health.items()},
        "db": db,
        "users_with_more_than_one_booking": int(dup_keys),
        "checks": [
            {"check": "Bookings succeeded during the failures", "pass": finals.get(201, 0) > 0},
            {"check": "No request ended in a 5xx/connection error after controlled retries",
             "pass": all(k in (201, 409) for k in finals)},
            {"check": "No seat booked twice", "pass": db["db_duplicate_seat_assignments"] == 0},
            {"check": "Retries never created a second booking for a request", "pass": dup_keys == 0},
            {"check": "Client-visible successes == PostgreSQL bookings", "pass": db["client_vs_db_booking_ids_match"]},
            {"check": "Seat/booking cross-references consistent",
             "pass": db["db_booked_seat_without_booking"] == 0 and db["db_booking_without_booked_seat"] == 0},
        ],
    }
    summary["all_checks_passed"] = all(c["pass"] for c in summary["checks"])
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"failure_demo-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps({"summary": summary, "requests": log}, indent=2, default=str))
    (REPORTS / "latest_failure_demo.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: summary[k] for k in ("requests", "final_status_counts", "requests_needing_client_retry",
                                               "nginx_transparent_failovers", "served_by_node", "node_a_restarted",
                                               "all_checks_passed", "checks")}, indent=2))
    print("report:", out)


if __name__ == "__main__":
    asyncio.run(main())
