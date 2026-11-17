"""Administrator and development-only endpoints.

Every route here needs X-Admin-Token. Destructive/dev routes (reset, seed, benchmark,
failure injection, naive demo) are additionally disabled when APP_ENV=production or
ENABLE_DEV_ENDPOINTS=false.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session, get_sessionmaker
from app.core.errors import ApiError
from app.core.redis_client import REDIS_ERRORS, get_redis
from app.core.security import require_admin, require_dev_admin
from app.distributed import failure
from app.monitoring.events import EventType, recorder
from app.schemas import AdminResetIn, BenchmarkIn, DelayIn, NaiveBookIn, UnhealthyIn
from app.services import seed_service

log = logging.getLogger("tatkalx.admin")
router = APIRouter(prefix="/api/admin")
_BENCH_LOCK = "tatkalx:benchmark:running"
_bench_tasks: set[asyncio.Task] = set()


# ------------------------------------------------------------------ observability
@router.get("/booking-events", dependencies=[Depends(require_admin)], summary="Recent booking events")
async def booking_events(
    limit: int = Query(100, ge=1, le=1000),
    event_type: str | None = Query(None, max_length=32),
    node_id: str | None = Query(None, max_length=16),
    journey_id: int | None = None,
    s: AsyncSession = Depends(get_session),
):
    where, params = ["true"], {"lim": limit}
    if event_type:
        where.append("event_type = :et"); params["et"] = event_type
    if node_id:
        where.append("node_id = :nid"); params["nid"] = node_id
    if journey_id:
        where.append("journey_id = :jid"); params["jid"] = journey_id
    rows = (await s.execute(text(
        "SELECT id, event_timestamp, node_id, event_type, outcome, request_id, booking_id, journey_id, metadata "
        f"FROM booking_events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT :lim"), params)).mappings().all()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ data management
@router.post("/reset-test-data", dependencies=[Depends(require_dev_admin)], summary="Create a fresh TEST journey")
async def reset_test_data(body: AdminResetIn):
    async with get_sessionmaker()() as s, s.begin():
        out = await seed_service.reset_test_journey(s, body.seats, label=f"{body.scenario}-{uuid.uuid4().hex[:8]}")
    out.pop("seat_ids")
    return out


@router.post("/seed", dependencies=[Depends(require_dev_admin)], summary="Seed simulated trains/journeys")
async def seed(force: bool = False):
    async with get_sessionmaker()() as s, s.begin():
        out = await seed_service.seed(s, force=force)
    try:
        await get_redis().delete("tatkalx:cache:trains")
    except REDIS_ERRORS:
        pass
    return out


# ------------------------------------------------------------------ benchmark
@router.post("/benchmark", status_code=202, dependencies=[Depends(require_dev_admin)],
             summary="Run a concurrency benchmark through the load balancer (background)")
async def run_benchmark(body: BenchmarkIn):
    st = get_settings()
    run_id = f"{body.scenario}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"
    try:
        if not await get_redis().set(_BENCH_LOCK, run_id, nx=True, ex=600):
            raise ApiError(409, "BENCHMARK_RUNNING", "Another benchmark is already running")
    except REDIS_ERRORS:
        raise ApiError(503, "COORDINATION_UNAVAILABLE", "Redis is required to coordinate benchmark runs")
    root = Path(__file__).resolve().parents[3]
    reports = Path(st.reports_dir)
    if not reports.is_absolute():
        reports = (Path.cwd() / reports).resolve()

    async def _run():
        # The load generator runs as a SEPARATE process so it does not compete with this node's
        # event loop for CPU (which would inflate this node's latency numbers).
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(root / "load_testing" / "benchmark.py"),
                "--scenario", body.scenario, "--requests", str(body.requests), "--seats", str(body.seats),
                "--allocation", body.allocation, "--base-url", st.load_balancer_url, "--admin-token", st.admin_token,
                "--database-url", st.database_url, "--reports-dir", str(reports), "--run-id", run_id,
                "--triggered-by", f"dashboard via node {st.node_id}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=600)
            if proc.returncode not in (0, 1):
                log.error("benchmark %s exited %s: %s", run_id, proc.returncode, err.decode()[-500:])
                async with get_sessionmaker()() as s, s.begin():
                    await s.execute(text("INSERT INTO benchmark_runs (id, scenario, status) VALUES (:i, :s, 'ERROR') "
                                         "ON CONFLICT (id) DO UPDATE SET status='ERROR', finished_at=now()"),
                                    {"i": run_id, "s": body.scenario})
        except Exception:
            log.exception("benchmark %s failed", run_id)
        finally:
            try:
                await get_redis().delete(_BENCH_LOCK)
            except REDIS_ERRORS:
                pass

    task = asyncio.create_task(_run())
    _bench_tasks.add(task)
    task.add_done_callback(_bench_tasks.discard)
    return {"run_id": run_id, "status": "RUNNING", "triggered_on_node": st.node_id}


@router.get("/benchmark/runs", dependencies=[Depends(require_admin)], summary="List benchmark runs")
async def benchmark_runs(limit: int = Query(20, ge=1, le=100), s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(text(
        "SELECT id, scenario, status, started_at, finished_at, summary FROM benchmark_runs "
        "ORDER BY started_at DESC LIMIT :l"), {"l": limit})).mappings().all()
    return [dict(r) for r in rows]


@router.get("/benchmark/runs/{run_id}", dependencies=[Depends(require_admin)])
async def benchmark_run(run_id: str, s: AsyncSession = Depends(get_session)):
    r = (await s.execute(text("SELECT * FROM benchmark_runs WHERE id = :id"), {"id": run_id})).mappings().first()
    if r is None:
        raise ApiError(404, "RUN_NOT_FOUND", "Benchmark run not found")
    return dict(r)


@router.get("/benchmark/runs/{run_id}/report", dependencies=[Depends(require_admin)],
            summary="Download the full per-request report (format=json|csv)")
async def benchmark_report(run_id: str, format: str = Query("json", pattern="^(json|csv)$")):
    if not all(c.isalnum() or c in "-_" for c in run_id):
        raise ApiError(400, "BAD_RUN_ID", "Invalid run id")
    reports = Path(get_settings().reports_dir)
    if not reports.is_absolute():
        reports = (Path.cwd() / reports).resolve()
    path = reports / f"{run_id}.{format}"
    if not path.is_file():
        raise ApiError(404, "REPORT_NOT_FOUND", "Report file not found on this node's reports directory")
    return FileResponse(path, media_type="text/csv" if format == "csv" else "application/json", filename=path.name)


# ------------------------------------------------------------------ failure injection (dev only)
@router.get("/failure/state", dependencies=[Depends(require_admin)])
async def failure_state():
    return {"node_id": get_settings().node_id, "unhealthy_remaining_s": round(failure.unhealthy_remaining(), 1),
            "delay_ms": await failure.current_delay_ms()}


@router.post("/failure/crash", dependencies=[Depends(require_dev_admin)],
             summary="Kill THIS node's process (exit 75). Reach a specific node via /node/{a|b|c}/api/...")
async def crash():
    node = get_settings().node_id
    recorder.record(EventType.FAILURE_INJECTED, outcome="CRASH", kind="process_exit")
    recorder.record(EventType.NODE_STOPPING, outcome="CRASH")
    await recorder.stop()
    log.warning("FAILURE INJECTION: node %s exiting with code 75", node)
    asyncio.get_running_loop().call_later(0.2, os._exit, 75)
    return {"node_id": node, "action": "crashing", "note": "process exits in 200 ms; supervisor restarts it"}


@router.post("/failure/unhealthy", dependencies=[Depends(require_dev_admin)], summary="Brown-out THIS node (503s)")
async def make_unhealthy(body: UnhealthyIn):
    failure.set_unhealthy(body.seconds)
    recorder.record(EventType.FAILURE_INJECTED, outcome="UNHEALTHY", seconds=body.seconds)
    return {"node_id": get_settings().node_id, "unhealthy_for_s": body.seconds}


@router.delete("/failure/unhealthy", dependencies=[Depends(require_dev_admin)])
async def clear_unhealthy():
    failure.clear_unhealthy()
    return {"node_id": get_settings().node_id, "unhealthy_for_s": 0}


@router.post("/failure/delay", dependencies=[Depends(require_dev_admin)],
             summary="Cluster-wide delay inside the booking transaction (widens the race window)")
async def set_delay(body: DelayIn):
    await failure.set_delay(body.delay_ms, body.ttl_s)
    recorder.record(EventType.FAILURE_INJECTED, outcome="DELAY", delay_ms=body.delay_ms, ttl_s=body.ttl_s)
    return {"delay_ms": body.delay_ms, "ttl_s": body.ttl_s}


@router.delete("/failure/delay", dependencies=[Depends(require_dev_admin)])
async def clear_delay():
    await failure.clear_delay()
    return {"delay_ms": 0}


# ------------------------------------------------------------------ naive race demonstration (dev only)
@router.post("/naive/reset", dependencies=[Depends(require_dev_admin)],
             summary="Reset the UNPROTECTED demo table (1 seat)")
async def naive_reset():
    async with get_sessionmaker()() as s, s.begin():
        seat_id = await seed_service.reset_naive_demo(s)
    return {"seat_id": seat_id}


@router.post("/naive/book", dependencies=[Depends(require_dev_admin)],
             summary="Check-then-write booking WITHOUT protection (to show the race)")
async def naive_book(body: NaiveBookIn):
    """Anti-pattern on purpose: SELECT status, 'process', then INSERT + UPDATE with no condition.
    Real bookings never use this; it writes only to naive_demo_* tables."""
    node = get_settings().node_id
    async with get_sessionmaker()() as s:
        status = (await s.execute(text("SELECT status FROM naive_demo_seats WHERE id = :id"), {"id": body.seat_id})).scalar_one_or_none()
        await s.commit()
        if status != "AVAILABLE":
            return {"booked": False, "node_id": node}
        await asyncio.sleep(random.uniform(0.0, 0.02))  # e.g. fare calculation / payment call
        await s.execute(text("INSERT INTO naive_demo_bookings (seat_id, user_id, node_id) VALUES (:s, :u, :n)"),
                        {"s": body.seat_id, "u": body.user_id, "n": node})
        await s.execute(text("UPDATE naive_demo_seats SET status = 'BOOKED' WHERE id = :id"), {"id": body.seat_id})
        await s.commit()
    return {"booked": True, "node_id": node}
