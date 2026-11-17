"""Public REST API (trains, journeys, seats, bookings, health, metrics)."""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.errors import ApiError
from app.core.redis_client import REDIS_ERRORS, get_redis
from app.core.security import current_user, is_admin
from app.distributed import failure
from app.distributed.sharding import seat_lock_key, shard_home_node
from app.monitoring import node_state
from app.monitoring.events import recorder
from app.repositories import BookingRepository, JourneyRepository, SeatRepository
from app.schemas import BookingCreate, BookingOut, JourneyOut, SeatMapOut, TrainOut
from app.services.booking_service import BookingService
from app.services.metrics_service import cluster_metrics

router = APIRouter(prefix="/api")
_IDEM_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,80}$")


async def _cached(key: str, ttl: int, producer):
    """Read-through Redis cache for read-mostly listings. Falls back to the DB if Redis is down."""
    r = get_redis()
    try:
        hit = await r.get(key)
        if hit:
            return json.loads(hit)
    except REDIS_ERRORS:
        return await producer()
    value = await producer()
    try:
        await r.set(key, json.dumps(value, default=str), ex=ttl)
    except REDIS_ERRORS:
        pass
    return value


@router.get("/trains", response_model=list[TrainOut], summary="List simulated trains")
async def list_trains(s: AsyncSession = Depends(get_session)):
    async def produce():
        rows = (await s.execute(text("SELECT * FROM trains ORDER BY train_number"))).mappings().all()
        return [TrainOut.model_validate(dict(r)).model_dump(mode="json") for r in rows]

    return await _cached("tatkalx:cache:trains", 60, produce)


@router.get("/journeys", response_model=list[JourneyOut], summary="Search journeys with live seat counts")
async def list_journeys(
    source: str | None = Query(None, max_length=80),
    destination: str | None = Query(None, max_length=80),
    journey_date: date | None = Query(None, alias="date"),
    include_test: bool = False,
    s: AsyncSession = Depends(get_session),
):
    rows = await JourneyRepository.search(s, source, destination, journey_date, include_test)
    return [JourneyOut.model_validate(r) for r in rows]


@router.get("/journeys/{journey_id}/seats", response_model=SeatMapOut, summary="Seat map (AVAILABLE/HELD/BOOKED/BLOCKED)")
async def journey_seats(journey_id: int, s: AsyncSession = Depends(get_session)):
    journey = await JourneyRepository.get(s, journey_id)
    if journey is None:
        raise ApiError(404, "JOURNEY_NOT_FOUND", "Journey not found")
    seats = await SeatRepository.seats_for_journey(s, journey_id)
    # Overlay HELD: an AVAILABLE seat whose Redis lock is currently held has a booking in flight.
    available = [x for x in seats if x["status"] == "AVAILABLE"]
    if available and get_settings().coordination_mode == "redis_lock":
        try:
            held = await get_redis().mget([seat_lock_key(x["shard_id"], journey_id, x["id"]) for x in available])
            for seat, h in zip(available, held):
                if h:
                    seat["status"] = "HELD"
        except REDIS_ERRORS:
            pass
    coaches = sorted({x["coach_number"] for x in seats})
    return SeatMapOut(journey=JourneyOut.model_validate(journey), coaches=coaches, seats=seats,
                      coordination_mode=get_settings().coordination_mode)


@router.post(
    "/bookings",
    status_code=201,
    summary="Book a seat (requires Idempotency-Key header)",
    responses={
        201: {"description": "Booking confirmed"},
        409: {"description": "SEAT_UNAVAILABLE | SEAT_BUSY | SOLD_OUT | REQUEST_IN_PROGRESS"},
        422: {"description": "Validation error or IDEMPOTENCY_KEY_REUSED"},
        429: {"description": "RATE_LIMITED"},
        503: {"description": "SERVICE_UNAVAILABLE (safe to retry with same key)"},
    },
)
async def create_booking(
    body: BookingCreate,
    request: Request,
    user_id: str = Depends(current_user),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    if not _IDEM_RE.match(idempotency_key):
        raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 8-80 chars [A-Za-z0-9_-:.]")
    result = await BookingService().create_booking(
        user_id=user_id, idem_key=idempotency_key, body=body,
        client_ip=request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown"),
        request_id=request.state.request_id,
    )
    return JSONResponse(result.payload, status_code=result.status, headers=result.headers)


@router.get("/bookings/{booking_id}", response_model=BookingOut, summary="Get one booking (owner or admin)")
async def get_booking(
    booking_id: uuid.UUID,
    s: AsyncSession = Depends(get_session),
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
):
    b = await BookingRepository.get(s, booking_id)
    if b is None or (b["user_id"] != x_user_id and not is_admin(x_admin_token)):
        raise ApiError(404, "BOOKING_NOT_FOUND", "Booking not found")
    return b


@router.get("/users/{user_id}/bookings", response_model=list[BookingOut], summary="Booking history for a user")
async def user_bookings(
    user_id: str,
    s: AsyncSession = Depends(get_session),
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
):
    if user_id != x_user_id and not is_admin(x_admin_token):
        raise ApiError(403, "FORBIDDEN", "You can only view your own bookings")
    return await BookingRepository.for_user(s, user_id)


@router.get("/health", summary="Node health: DB, Redis, injected faults")
async def health(s: AsyncSession = Depends(get_session)):
    st = get_settings()
    out = {"node_id": st.node_id, "pid": node_state.PID, "uptime_s": round(time.time() - node_state.STARTED_AT, 1),
           "coordination_mode": st.coordination_mode, "app_env": st.app_env}
    t0 = time.perf_counter()
    try:
        await s.execute(text("SELECT 1"))
        out["database"] = {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
    except Exception as exc:
        out["database"] = {"ok": False, "error": type(exc).__name__}
    t0 = time.perf_counter()
    try:
        await get_redis().ping()
        out["redis"] = {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
        node_state.mark_redis(True)
    except REDIS_ERRORS as exc:
        out["redis"] = {"ok": False, "error": type(exc).__name__}
        node_state.mark_redis(False)
    out["injected"] = {"unhealthy_remaining_s": round(failure.unhealthy_remaining(), 1),
                       "delay_ms": await failure.current_delay_ms()}
    out["events"] = {"written": recorder.written, "dropped": recorder.dropped, "backlog": recorder.backlog}
    if not out["database"]["ok"]:
        out["status"] = "unhealthy"
    elif not out["redis"]["ok"]:
        out["status"] = "degraded"  # bookings still correct (DB authoritative), locks/rate limits skipped
    else:
        out["status"] = "ok"
    return JSONResponse(out, status_code=200 if out["status"] != "unhealthy" else 503)


@router.get("/metrics", summary="Cluster metrics from the shared event log and inventory")
async def metrics(window_minutes: int = Query(60, ge=1, le=1440), s: AsyncSession = Depends(get_session)):
    data = await cluster_metrics(s, window_minutes)
    data["shard_home_nodes"] = {i: shard_home_node(i) for i in range(1, get_settings().num_shards + 1)}
    return data
