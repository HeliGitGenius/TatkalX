"""TatkalX concurrency benchmark.

Fires N simultaneous booking requests THROUGH THE LOAD BALANCER (so they spread across
Node A/B/C), records every response, then verifies the outcome directly in PostgreSQL.
Nothing is hard-coded: every number in the summary comes from real responses or SQL.

Scenarios
  one_seat     1 seat, N requests with distinct users/keys           -> exactly 1 success
  limited      S seats, N requests (targeted: request i -> seat i%S,  -> exactly min(S, N) successes,
               or auto: server allocates with SKIP LOCKED)              no seat twice
  idempotency  N/5 logical bookings, each sent 5x at once with the     -> one booking per logical request,
               SAME Idempotency-Key, then retried once more              retries replay the original
  rate_limit   one user sends N requests at once                       -> exactly RATE_LIMIT_PER_USER succeed
  naive_race   N requests against the deliberately UNPROTECTED          -> shows double booking happening
               check-then-write endpoint (separate demo table)          when protections are absent

Usage
  python load_testing/benchmark.py --scenario one_seat --requests 500
  python load_testing/benchmark.py --scenario limited --seats 50 --allocation targeted
  python load_testing/benchmark.py --suite            # run every scenario in sequence
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx

CONFLICT_CODES = {"SEAT_UNAVAILABLE", "SEAT_BUSY", "SOLD_OUT"}
DEFAULT_REPORTS = Path(__file__).resolve().parent / "reports"


# ----------------------------------------------------------------------------- helpers
def _pg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)


def classify(status: int | None, code: str | None, replayed: bool) -> str:
    if status is None:
        return "unexpected_error"
    if replayed:
        return "duplicate_replayed"
    if status == 201:
        return "success"
    if status == 409 and code in CONFLICT_CODES:
        return "seat_unavailable"
    if status == 409 and code == "REQUEST_IN_PROGRESS":
        return "duplicate_in_progress"
    if status == 429:
        return "rate_limited"
    if status in (400, 404, 422):
        return "validation_error"
    return "unexpected_error"


async def _send(client: httpx.AsyncClient, idx: int, method: str, path: str, body: dict, headers: dict,
                start: asyncio.Event, phase: str) -> dict[str, Any]:
    await start.wait()
    t0 = time.perf_counter()
    rec: dict[str, Any] = {"idx": idx, "phase": phase, "user_id": headers.get("X-User-Id"),
                           "idempotency_key": headers.get("Idempotency-Key"), "target_seat": body.get("seat_id")}
    try:
        r = await client.request(method, path, json=body, headers=headers)
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        rec["status"] = r.status_code
        rec["node"] = r.headers.get("x-served-by")
        rec["upstream"] = r.headers.get("x-upstream-addr")
        rec["replayed"] = r.headers.get("idempotent-replayed") == "true"
        try:
            data = r.json()
        except ValueError:
            data = {}
        rec["code"] = (data.get("error") or {}).get("code") if isinstance(data, dict) else None
        rec["booking_id"] = data.get("booking_id") if isinstance(data, dict) else None
        rec["seat_id"] = (data.get("seat") or {}).get("id") if isinstance(data, dict) and data.get("seat") else None
        rec["booked"] = data.get("booked") if isinstance(data, dict) else None
        rec["error"] = None
    except Exception as exc:  # network error, timeout ... counted as unexpected, never hidden
        rec.update(latency_ms=round((time.perf_counter() - t0) * 1000, 2), status=None, node=None, upstream=None,
                   replayed=False, code=None, booking_id=None, seat_id=None, booked=None,
                   error=f"{type(exc).__name__}: {exc}"[:200])
    rec["outcome"] = classify(rec["status"], rec["code"], rec["replayed"])
    return rec


async def _burst(client, jobs: list[tuple[str, str, dict, dict]], phase: str) -> tuple[list[dict], float]:
    """Create all requests first, then release them together so they truly overlap."""
    start = asyncio.Event()
    tasks = [asyncio.create_task(_send(client, i, m, p, b, h, start, phase)) for i, (m, p, b, h) in enumerate(jobs)]
    await asyncio.sleep(0.2)
    t0 = time.perf_counter()
    start.set()
    results = await asyncio.gather(*tasks)
    return list(results), time.perf_counter() - t0


def _passenger(i: int) -> dict:
    return {"name": f"Sim Passenger {chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}", "age": 18 + i % 60,
            "gender": "MFO"[i % 3]}


# ----------------------------------------------------------------------------- verification
async def verify_in_postgres(dsn: str, journey_id: int, client_success_ids: set[str]) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn)
    try:
        confirmed = await conn.fetch(
            "SELECT id::text, journey_seat_id FROM bookings WHERE journey_id = $1 AND status = 'CONFIRMED'", journey_id)
        dup_seats = await conn.fetchval(
            "SELECT count(*) FROM (SELECT journey_seat_id FROM bookings WHERE journey_id=$1 AND status='CONFIRMED' "
            "GROUP BY 1 HAVING count(*) > 1) d", journey_id)
        seat_counts = dict(await conn.fetch(
            "SELECT status, count(*) FROM journey_seats WHERE journey_id = $1 GROUP BY 1", journey_id))
        booked_without_booking = await conn.fetchval(
            "SELECT count(*) FROM journey_seats s WHERE s.journey_id=$1 AND s.status='BOOKED' AND NOT EXISTS "
            "(SELECT 1 FROM bookings b WHERE b.id=s.booking_id AND b.journey_seat_id=s.id AND b.status='CONFIRMED')",
            journey_id)
        booking_without_seat = await conn.fetchval(
            "SELECT count(*) FROM bookings b WHERE b.journey_id=$1 AND b.status='CONFIRMED' AND NOT EXISTS "
            "(SELECT 1 FROM journey_seats s WHERE s.id=b.journey_seat_id AND s.booking_id=b.id AND s.status='BOOKED')",
            journey_id)
        await asyncio.sleep(1.0)  # let nodes flush their buffered audit events
        node_events = dict(await conn.fetch(
            "SELECT node_id, count(*) FROM booking_events WHERE journey_id=$1 AND event_type='REQUEST_RECEIVED' "
            "GROUP BY 1 ORDER BY 1", journey_id))
        lock_events = dict(await conn.fetch(
            "SELECT event_type, count(*) FROM booking_events WHERE journey_id=$1 AND event_type LIKE 'LOCK_%' "
            "GROUP BY 1", journey_id))
    finally:
        await conn.close()
    db_ids = {r["id"] for r in confirmed}
    return {
        "db_confirmed_bookings": len(confirmed),
        "db_unique_seats_booked": len({r["journey_seat_id"] for r in confirmed}),
        "db_duplicate_seat_assignments": int(dup_seats),
        "db_seat_status_counts": {k: int(v) for k, v in seat_counts.items()},
        "db_booked_seat_without_booking": int(booked_without_booking),
        "db_booking_without_booked_seat": int(booking_without_seat),
        "client_vs_db_booking_ids_match": db_ids == client_success_ids,
        "bookings_in_db_not_reported_to_clients": len(db_ids - client_success_ids),
        "client_successes_missing_in_db": len(client_success_ids - db_ids),
        "db_requests_received_by_node": {k: int(v) for k, v in node_events.items()},
        "db_lock_events": {k: int(v) for k, v in lock_events.items()},
    }


# ----------------------------------------------------------------------------- main runner
async def run_benchmark(*, base_url: str, admin_token: str, database_url: str, scenario: str = "one_seat",
                        requests: int = 500, seats: int = 50, allocation: str = "targeted",
                        reports_dir: str | os.PathLike = DEFAULT_REPORTS, run_id: str | None = None,
                        triggered_by: str = "cli") -> dict[str, Any]:
    run_id = run_id or f"{scenario}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"
    dsn = _pg_dsn(database_url)
    reports = Path(reports_dir)
    if not reports.is_absolute():
        reports = (Path.cwd() / reports).resolve()
    reports.mkdir(parents=True, exist_ok=True)
    admin = {"X-Admin-Token": admin_token}
    conn = await asyncpg.connect(dsn)
    await conn.execute("INSERT INTO benchmark_runs (id, scenario, status) VALUES ($1, $2, 'RUNNING') "
                       "ON CONFLICT (id) DO NOTHING", run_id, scenario)
    await conn.close()

    limits = httpx.Limits(max_connections=max(requests, 10) + 50, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0), limits=limits) as client:
        cfg = (await client.get("/api/metrics", params={"window_minutes": 1})).json()["config"]
        jobs: list[tuple[str, str, dict, dict]] = []
        journey_id = None
        seat_ids: list[int] = []
        expected_success: int | None = None
        uid = run_id[-4:]

        if scenario == "naive_race":
            seat = (await client.post("/api/admin/naive/reset", headers=admin)).json()["seat_id"]
            for i in range(requests):
                jobs.append(("POST", "/api/admin/naive/book", {"seat_id": seat, "user_id": f"naive-{uid}-{i}"}, admin))
        else:
            n_seats = {"one_seat": 1, "limited": seats, "idempotency": seats, "rate_limit": requests}[scenario]
            setup = await client.post("/api/admin/reset-test-data", headers=admin, json={"scenario": scenario, "seats": n_seats})
            setup.raise_for_status()
            journey_id = setup.json()["journey_id"]
            seat_ids = [s["id"] for s in (await client.get(f"/api/journeys/{journey_id}/seats")).json()["seats"]]

            if scenario in ("one_seat", "limited"):
                auto = scenario == "limited" and allocation == "auto"
                for i in range(requests):
                    body = {"journey_id": journey_id, "passenger": _passenger(i)}
                    if auto:
                        body["auto_allocate"] = True
                    else:
                        body["seat_id"] = seat_ids[i % len(seat_ids)]
                    jobs.append(("POST", "/api/bookings", body,
                                 {"X-User-Id": f"bench-{uid}-u{i}", "Idempotency-Key": f"bench-{uid}-k{i}"}))
                expected_success = min(len(seat_ids), requests)
            elif scenario == "idempotency":
                logical = max(1, requests // 5)
                for i in range(logical):
                    body = {"journey_id": journey_id, "seat_id": seat_ids[i % len(seat_ids)], "passenger": _passenger(i)}
                    h = {"X-User-Id": f"bench-{uid}-u{i}", "Idempotency-Key": f"bench-{uid}-logical-{i}"}
                    jobs.extend([("POST", "/api/bookings", body, h)] * 5)
                expected_success = min(logical, len(seat_ids))
            elif scenario == "rate_limit":
                for i in range(requests):
                    jobs.append(("POST", "/api/bookings", {"journey_id": journey_id, "auto_allocate": True,
                                                           "passenger": _passenger(i)},
                                 {"X-User-Id": f"bench-{uid}-single-user", "Idempotency-Key": f"bench-{uid}-rl-{i}"}))
                expected_success = min(cfg["rate_limit_per_user"], requests) if cfg["rate_limit_enabled"] else requests

        results, duration = await _burst(client, jobs, "burst")
        retry_results: list[dict] = []
        if scenario == "idempotency":  # every client "retries" once after a lost response
            await asyncio.sleep(0.5)
            retry_jobs = [jobs[i] for i in range(0, len(jobs), 5)]
            retry_results, _ = await _burst(client, retry_jobs, "retry")

    if scenario == "naive_race":  # the demo endpoint answers 200 {"booked": true|false}
        for r in results:
            if r["status"] == 200:
                r["outcome"] = "success" if r.get("booked") else "seat_unavailable"
                r["code"] = None if r.get("booked") else "SEAT_UNAVAILABLE"

    # ------------------------------------------------------------------ metrics from responses
    outcomes = Counter(r["outcome"] for r in results)
    nodes = Counter(r["node"] or "none" for r in results)
    lat = [r["latency_ms"] for r in results if r["status"] is not None]
    success_ids = {r["booking_id"] for r in results + retry_results if r["outcome"] == "success" and r["booking_id"]}
    summary: dict[str, Any] = {
        "run_id": run_id,
        "scenario": scenario,
        "allocation": allocation if scenario == "limited" else ("targeted" if scenario in ("one_seat", "idempotency") else "auto"),
        "triggered_by": triggered_by,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "coordination_mode": cfg["coordination_mode"],
        "rate_limit_test_mode": cfg["rate_limit_test_mode"],
        "journey_id": journey_id,
        "seats_available_at_start": len(seat_ids) if scenario != "naive_race" else 1,
        "total_requests": len(results),
        "successful_bookings": outcomes["success"],
        "seat_unavailable_responses": outcomes["seat_unavailable"],
        "seat_unavailable_by_code": dict(Counter(r["code"] for r in results if r["outcome"] == "seat_unavailable")),
        "duplicate_request_responses": outcomes["duplicate_replayed"] + outcomes["duplicate_in_progress"],
        "duplicate_replayed": outcomes["duplicate_replayed"],
        "duplicate_in_progress": outcomes["duplicate_in_progress"],
        "rate_limited_responses": outcomes["rate_limited"],
        "validation_errors": outcomes["validation_error"],
        "unexpected_errors": outcomes["unexpected_error"],
        "unexpected_error_samples": [r["error"] or f"HTTP {r['status']} {r['code']}" for r in results
                                     if r["outcome"] == "unexpected_error"][:5],
        "requests_by_node": {k: nodes.get(k, 0) for k in sorted(set(nodes) | {"A", "B", "C"})},
        "latency_ms": {"avg": round(statistics.fmean(lat), 2) if lat else None, "median": _pct(lat, 0.5),
                       "p95": _pct(lat, 0.95), "p99": _pct(lat, 0.99), "max": max(lat) if lat else None,
                       "min": min(lat) if lat else None},
        "duration_s": round(duration, 3),
        "throughput_rps": round(len(results) / duration, 1) if duration else None,
        "expected_successes": expected_success,
        # compact per-request outcome list for the dashboard's request grid: [node, outcome]
        "request_grid": [[r["node"] or "-", r["outcome"]] for r in results],
    }
    if retry_results:
        summary["retry_pass"] = dict(Counter(r["outcome"] for r in retry_results))
        same = 0
        by_key: dict[str, set] = {}
        for r in results + retry_results:
            if r["booking_id"]:
                by_key.setdefault(r["idempotency_key"], set()).add(r["booking_id"])
        same = sum(1 for v in by_key.values() if len(v) == 1)
        summary["logical_requests"] = len(jobs) // 5
        summary["keys_with_exactly_one_booking_id"] = same
        summary["keys_with_multiple_booking_ids"] = sum(1 for v in by_key.values() if len(v) > 1)

    # ------------------------------------------------------------------ verification in PostgreSQL
    checks: list[dict[str, Any]] = []

    def check(name: str, expected: Any, actual: Any, ok: bool) -> None:
        checks.append({"check": name, "expected": expected, "actual": actual, "pass": bool(ok)})

    if scenario == "naive_race":
        conn = await asyncpg.connect(dsn)
        try:
            n = await conn.fetchval("SELECT count(*) FROM naive_demo_bookings")
            seats_booked = await conn.fetchval("SELECT count(DISTINCT seat_id) FROM naive_demo_bookings")
        finally:
            await conn.close()
        reported = sum(1 for r in results if r.get("booked"))
        summary["successful_bookings"] = reported
        summary["db"] = {"naive_bookings_for_one_seat": int(n), "distinct_seats": int(seats_booked)}
        summary["unique_seats_booked"] = int(seats_booked)
        summary["duplicate_seat_assignments"] = max(0, int(n) - int(seats_booked))
        check("UNPROTECTED design books the single seat at most once", 1, int(n), n <= 1)
        summary["note"] = ("Expected to FAIL: this endpoint deliberately uses check-then-write without a conditional "
                           "UPDATE or unique constraint, to show the race the real booking path prevents.")
    else:
        db = await verify_in_postgres(dsn, journey_id, success_ids)
        summary["db"] = db
        summary["unique_seats_booked"] = db["db_unique_seats_booked"]
        summary["duplicate_seat_assignments"] = db["db_duplicate_seat_assignments"]
        clean = outcomes["unexpected_error"] == 0
        check("No seat has two CONFIRMED bookings", 0, db["db_duplicate_seat_assignments"], db["db_duplicate_seat_assignments"] == 0)
        check("Confirmed bookings never exceed seats", f"<= {len(seat_ids)}", db["db_confirmed_bookings"],
              db["db_confirmed_bookings"] <= len(seat_ids))
        check("Every BOOKED seat has its CONFIRMED booking", 0, db["db_booked_seat_without_booking"],
              db["db_booked_seat_without_booking"] == 0)
        check("Every CONFIRMED booking owns a BOOKED seat", 0, db["db_booking_without_booked_seat"],
              db["db_booking_without_booked_seat"] == 0)
        check("Booking IDs returned to clients == booking IDs in PostgreSQL", True, db["client_vs_db_booking_ids_match"],
              db["client_vs_db_booking_ids_match"])
        check("Unexpected infrastructure errors", 0, outcomes["unexpected_error"], clean)
        if expected_success is not None:
            check("Successful bookings (DB)", expected_success, db["db_confirmed_bookings"],
                  db["db_confirmed_bookings"] == expected_success or (not clean and db["db_confirmed_bookings"] <= expected_success))
        if scenario == "one_seat":
            check("Final seat state", "BOOKED", next(iter(db["db_seat_status_counts"])) if db["db_seat_status_counts"] else None,
                  db["db_seat_status_counts"].get("BOOKED") == 1)
        if scenario == "idempotency":
            check("Each Idempotency-Key maps to at most one booking", 0, summary["keys_with_multiple_booking_ids"],
                  summary["keys_with_multiple_booking_ids"] == 0)
            check("Retry pass replays stored results", len(retry_results),
                  summary["retry_pass"].get("duplicate_replayed", 0),
                  summary["retry_pass"].get("duplicate_replayed", 0) == len(retry_results))
        if scenario == "rate_limit":
            check("Requests beyond the per-user limit get HTTP 429", len(results) - (expected_success or 0),
                  outcomes["rate_limited"], outcomes["rate_limited"] == len(results) - (expected_success or 0))
        if len({k for k, v in summary["requests_by_node"].items() if v and k != "none"}) < 3:
            summary["warning"] = "Fewer than 3 nodes served requests (a node may be down)."
    summary["checks"] = checks
    summary["all_checks_passed"] = all(c["pass"] for c in checks)
    summary["consistency_result"] = "CONSISTENT" if summary["all_checks_passed"] else (
        "INCONSISTENT (expected for naive_race)" if scenario == "naive_race" else "INCONSISTENT")
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------ export
    rows = results + retry_results
    (reports / f"{run_id}.json").write_text(json.dumps({"summary": summary, "requests": rows}, indent=2, default=str))
    fields = ["idx", "phase", "user_id", "idempotency_key", "target_seat", "status", "code", "outcome", "node",
              "upstream", "replayed", "booking_id", "seat_id", "booked", "latency_ms", "error"]
    with open(reports / f"{run_id}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    (reports / f"latest_{scenario}.json").write_text(json.dumps(summary, indent=2, default=str))
    summary["report_files"] = [f"{run_id}.json", f"{run_id}.csv"]

    conn = await asyncpg.connect(dsn)
    await conn.execute("UPDATE benchmark_runs SET status=$2, finished_at=now(), summary=$3::jsonb WHERE id=$1", run_id,
                       "PASSED" if summary["all_checks_passed"] else "FAILED", json.dumps(summary, default=str))
    await conn.close()
    return summary


def print_summary(s: dict[str, Any]) -> None:
    print(f"\n=== {s['run_id']}  [{s['scenario']} / {s['allocation']} / mode={s['coordination_mode']}] ===")
    keys = ["total_requests", "successful_bookings", "seat_unavailable_responses", "duplicate_request_responses",
            "rate_limited_responses", "unexpected_errors", "unique_seats_booked", "duplicate_seat_assignments",
            "requests_by_node", "latency_ms", "duration_s", "throughput_rps", "consistency_result"]
    for k in keys:
        print(f"  {k:30} {s.get(k)}")
    for c in s["checks"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['check']}: expected {c['expected']}, actual {c['actual']}")


async def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="one_seat",
                    choices=["one_seat", "limited", "idempotency", "rate_limit", "naive_race"])
    ap.add_argument("--requests", type=int, default=500)
    ap.add_argument("--seats", type=int, default=50)
    ap.add_argument("--allocation", choices=["targeted", "auto"], default="targeted")
    ap.add_argument("--suite", action="store_true", help="run all scenarios")
    ap.add_argument("--base-url", default=os.environ.get("LOAD_BALANCER_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", "change-me-admin-token"))
    ap.add_argument("--database-url", default=os.environ.get(
        "DATABASE_URL", "postgresql://tatkalx:tatkalx_dev_pw@127.0.0.1:5432/tatkalx"))
    ap.add_argument("--reports-dir", default=str(DEFAULT_REPORTS))
    ap.add_argument("--run-id", default=None, help="(used when started from the dashboard)")
    ap.add_argument("--triggered-by", default="cli")
    a = ap.parse_args()
    plan = [(a.scenario, a.seats, a.allocation)]
    if a.suite:
        plan = [("one_seat", 1, "targeted"), ("limited", a.seats, "targeted"), ("limited", a.seats, "auto"),
                ("idempotency", 100, "targeted"), ("rate_limit", 0, "auto"), ("naive_race", 1, "targeted")]
    failed = False
    for scenario, seats, allocation in plan:
        s = await run_benchmark(base_url=a.base_url, admin_token=a.admin_token, database_url=a.database_url,
                                scenario=scenario, requests=a.requests if scenario != "rate_limit" else min(a.requests, 60),
                                seats=seats, allocation=allocation, reports_dir=a.reports_dir,
                                run_id=None if a.suite else a.run_id, triggered_by=a.triggered_by)
        print_summary(s)
        failed |= (not s["all_checks_passed"]) and scenario != "naive_race"
        await asyncio.sleep(1)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(_cli())
