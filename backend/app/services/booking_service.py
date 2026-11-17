"""Booking service: the complete, ordered booking workflow.

    rate limit -> idempotency reservation -> seat pre-check -> Redis lock (optional)
      -> ONE PostgreSQL transaction { atomic seat claim ; insert booking ; store response }
      -> release lock -> respond

Correctness never depends on Redis. The transaction is what makes a booking real.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.errors import error_body
from app.core.redis_client import RedisUnavailable
from app.distributed import failure
from app.distributed.lock import DistributedLock, LockHandle
from app.distributed.rate_limiter import RateLimiter
from app.distributed.sharding import seat_lock_key
from app.monitoring import node_state
from app.monitoring.events import EventType, recorder
from app.repositories import BookingRepository, ClaimedSeat, IdempotencyRepository, JourneyRepository, SeatRepository
from app.schemas import BookingCreate, BookingOut

log = logging.getLogger("tatkalx.booking")

_rate_limiter = RateLimiter()


@dataclass
class BookingResult:
    status: int
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


class _FencedOut(Exception):
    """Our idempotency reservation was taken over by another attempt; abort our transaction."""


def request_fingerprint(user_id: str, body: BookingCreate) -> str:
    canonical = json.dumps(
        {
            "user": user_id,
            "journey_id": body.journey_id,
            "seat_id": body.seat_id,
            "auto": body.auto_allocate,
            "passenger": body.passenger.model_dump(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _new_pnr() -> str:
    return str(1_000_000_000 + secrets.randbelow(9_000_000_000))


def _lock() -> DistributedLock:
    s = get_settings()
    return DistributedLock(s.lock_ttl_ms, s.lock_wait_ms, s.lock_retry_interval_ms)


class BookingService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.node = self.settings.node_id

    def _err(self, status: int, code: str, message: str, request_id: uuid.UUID, **extra: Any) -> BookingResult:
        return BookingResult(status, error_body(code, message, node_id=self.node, request_id=str(request_id), **extra))

    # ------------------------------------------------------------------ public entry point
    async def create_booking(
        self, *, user_id: str, idem_key: str, body: BookingCreate, client_ip: str, request_id: uuid.UUID
    ) -> BookingResult:
        ev = dict(request_id=request_id, journey_id=body.journey_id)
        recorder.record(EventType.REQUEST_RECEIVED, **ev, seat_id=body.seat_id, auto=body.auto_allocate)

        # 1. Rate limiting --------------------------------------------------------------
        if self.settings.rate_limit_enabled:
            try:
                decision = await _rate_limiter.check(user_id, client_ip)
                node_state.mark_redis(True)
                if not decision.allowed:
                    recorder.record(EventType.RATE_LIMITED, **ev, outcome="429", scope=decision.scope, limit=decision.limit)
                    res = self._err(429, "RATE_LIMITED", f"Too many booking requests ({decision.scope} limit {decision.limit}/"
                                    f"{self.settings.rate_limit_window_s}s). Try again later.", request_id,
                                    scope=decision.scope, retry_after_s=decision.retry_after_s)
                    res.headers["Retry-After"] = str(decision.retry_after_s)
                    return res
            except RedisUnavailable:
                node_state.mark_redis(False)
                recorder.record(EventType.DEPENDENCY_FAILURE, **ev, outcome="RATE_LIMIT_SKIPPED", dependency="redis")

        fingerprint = request_fingerprint(user_id, body)
        token = uuid.uuid4()
        sm = get_sessionmaker()
        rec_id: uuid.UUID | None = None
        try:
            # 2. Idempotency reservation (own short transaction) -------------------------
            async with sm() as s, s.begin():
                rec_id = await IdempotencyRepository.reserve(
                    s, user_id=user_id, key=idem_key, fingerprint=fingerprint, token=token, node_id=self.node
                )
                if rec_id is None:
                    existing = await IdempotencyRepository.get(
                        s, user_id, idem_key, self.settings.idempotency_processing_timeout_s
                    )
                    early = await self._handle_existing(s, existing, fingerprint, token, request_id, ev)
                    if isinstance(early, BookingResult):
                        return early
                    rec_id = early  # stale PROCESSING record taken over

            # 3. The booking itself ----------------------------------------------------
            return await self._book(rec_id, token, user_id, body, request_id, ev)

        except (OperationalError, DBAPIError, PoolTimeout, OSError, asyncio.TimeoutError) as exc:
            log.error("booking infrastructure error request=%s: %s", request_id, type(exc).__name__)
            recorder.record(EventType.UNEXPECTED_ERROR, **ev, outcome="503", error=type(exc).__name__)
            if rec_id is not None:
                await self._release_reservation(rec_id, token)
            res = self._err(503, "SERVICE_UNAVAILABLE", "Booking could not be processed (database unavailable). "
                            "It is safe to retry with the same Idempotency-Key.", request_id)
            res.headers["Retry-After"] = "2"
            return res

    # ------------------------------------------------------------------ idempotency replay paths
    async def _handle_existing(self, s, existing, fingerprint, token, request_id, ev) -> BookingResult | uuid.UUID:
        if existing is None:  # deleted between INSERT and SELECT (released after an error) -> ask to retry
            return self._err(409, "REQUEST_IN_PROGRESS", "Request is being processed; retry shortly.", request_id)
        if existing.request_fingerprint != fingerprint:
            recorder.record(EventType.IDEMPOTENCY_CONFLICT, **ev, outcome="422")
            return self._err(422, "IDEMPOTENCY_KEY_REUSED",
                             "This Idempotency-Key was already used with a different booking request.", request_id)
        if existing.state == "COMPLETED":
            payload = existing.response_payload
            if isinstance(payload, str):
                payload = json.loads(payload)
            recorder.record(EventType.DUPLICATE_REQUEST, **ev, outcome=str(existing.response_status),
                            original_status=existing.response_status)
            return BookingResult(existing.response_status or 200, payload or {}, {"Idempotent-Replayed": "true"})
        if existing.stale and await IdempotencyRepository.take_over_stale(
            s, existing.id, token, self.node, self.settings.idempotency_processing_timeout_s
        ):
            recorder.record(EventType.DUPLICATE_REQUEST, **ev, outcome="TAKEOVER")
            return existing.id
        recorder.record(EventType.REQUEST_IN_PROGRESS, **ev, outcome="409")
        res = self._err(409, "REQUEST_IN_PROGRESS",
                        "An identical request with this Idempotency-Key is still being processed. Retry shortly.", request_id)
        res.headers["Retry-After"] = "1"
        return res

    async def _complete_only(self, rec_id: uuid.UUID, token: uuid.UUID, result: BookingResult) -> BookingResult:
        """Persist a non-booking outcome (e.g. 409 seat unavailable) so retries replay it."""
        async with get_sessionmaker()() as s, s.begin():
            await IdempotencyRepository.complete(s, rec_id=rec_id, token=token, status=result.status,
                                                 payload=result.payload, booking_id=None)
        return result

    async def _release_reservation(self, rec_id: uuid.UUID, token: uuid.UUID) -> None:
        try:
            async with get_sessionmaker()() as s, s.begin():
                await IdempotencyRepository.release(s, rec_id, token)
        except Exception:  # DB still down: the record goes stale and can be taken over later
            pass

    # ------------------------------------------------------------------ core booking
    async def _book(self, rec_id, token, user_id, body: BookingCreate, request_id, ev) -> BookingResult:
        sm = get_sessionmaker()

        # Read-only validation. The session is closed BEFORE we persist an early outcome, so a
        # request never holds one pooled connection while waiting for a second (pool deadlock).
        early: BookingResult | None = None
        seat = None
        async with sm() as s:
            journey = await JourneyRepository.get_basic(s, body.journey_id)
            if journey is None:
                early = self._err(404, "JOURNEY_NOT_FOUND", "Journey not found", request_id)
            elif journey["status"] != "SCHEDULED":
                early = self._err(409, "JOURNEY_CLOSED", "Journey is not open for booking", request_id)
            elif body.seat_id is not None:
                seat = await SeatRepository.get_seat(s, body.seat_id)
                if seat is None or seat["journey_id"] != body.journey_id:
                    early = self._err(404, "SEAT_NOT_FOUND", "Seat does not belong to this journey", request_id)
                elif seat["status"] != "AVAILABLE":
                    # Cheap optimistic pre-check. Safe: it can only reject seats that are already taken;
                    # an 'AVAILABLE' answer is never trusted -- the conditional UPDATE decides.
                    recorder.record(EventType.SEAT_UNAVAILABLE, **ev, outcome="409", stage="precheck", seat_id=body.seat_id)
                    early = self._seat_unavailable(request_id, body.seat_id)
        if early is not None:
            return await self._complete_only(rec_id, token, early)

        # Distributed lock (advisory) ---------------------------------------------------
        handle: LockHandle | None = None
        lock_mode = "db_only"
        if seat is not None and self.settings.coordination_mode == "redis_lock":
            key = seat_lock_key(seat["shard_id"], body.journey_id, seat["id"])
            try:
                handle = await _lock().acquire(key)
                node_state.mark_redis(True)
                lock_mode = "redis_lock"
                if handle is None:
                    recorder.record(EventType.LOCK_FAILED, **ev, outcome="SEAT_BUSY", seat_id=seat["id"], shard=seat["shard_id"])
                    async with sm() as s:
                        now = await SeatRepository.get_seat(s, seat["id"])
                    if now and now["status"] != "AVAILABLE":
                        return await self._complete_only(rec_id, token, self._seat_unavailable(request_id, seat["id"]))
                    return await self._complete_only(rec_id, token, self._err(
                        409, "SEAT_BUSY", "Another passenger is booking this seat right now. Please pick another seat.",
                        request_id, seat_id=seat["id"]))
                recorder.record(EventType.LOCK_ACQUIRED, **ev, seat_id=seat["id"], shard=seat["shard_id"],
                                wait_ms=round(handle.wait_ms, 2))
            except RedisUnavailable:
                node_state.mark_redis(False)
                recorder.record(EventType.DEPENDENCY_FAILURE, **ev, dependency="redis",
                                outcome="LOCK_SKIPPED" if self.settings.redis_failure_policy == "fail_open" else "503")
                if self.settings.redis_failure_policy == "fail_closed":
                    await self._release_reservation(rec_id, token)
                    return self._err(503, "COORDINATION_UNAVAILABLE", "Lock service unavailable; retry later.", request_id)
                lock_mode = "db_only (redis down)"

        try:
            return await self._transaction(rec_id, token, user_id, body, request_id, ev, journey, lock_mode)
        finally:
            if handle is not None:
                try:
                    released = await _lock().release(handle)
                    recorder.record(EventType.LOCK_RELEASED if released else EventType.LOCK_LEASE_LOST, **ev,
                                    seat_id=body.seat_id, held_ms=round(handle.ttl_ms - DistributedLock.lease_remaining_ms(handle), 2))
                except RedisUnavailable:
                    recorder.record(EventType.DEPENDENCY_FAILURE, **ev, dependency="redis", outcome="LOCK_RELEASE_FAILED")

    def _seat_unavailable(self, request_id, seat_id) -> BookingResult:
        return self._err(409, "SEAT_UNAVAILABLE", "Sorry, this seat has just been booked by another passenger.",
                         request_id, seat_id=seat_id)

    async def _transaction(self, rec_id, token, user_id, body: BookingCreate, request_id, ev, journey, lock_mode) -> BookingResult:
        sm = get_sessionmaker()
        booking_id = uuid.uuid4()
        delay_ms = await failure.current_delay_ms()
        try:
            async with sm() as s, s.begin():
                recorder.record(EventType.SEAT_CLAIM_ATTEMPTED, **ev, seat_id=body.seat_id, auto=body.auto_allocate)
                claimed: ClaimedSeat | None
                if body.auto_allocate:
                    claimed = await SeatRepository.claim_auto(s, body.journey_id, booking_id)
                else:
                    claimed = await SeatRepository.claim_specific(s, body.journey_id, body.seat_id, booking_id)

                if claimed is None:  # 0 rows updated -> someone else owns the seat
                    if body.auto_allocate:
                        result = self._err(409, "SOLD_OUT", "No seats left on this journey.", request_id)
                    else:
                        result = self._seat_unavailable(request_id, body.seat_id)
                    recorder.record(EventType.SEAT_UNAVAILABLE, **ev, outcome=result.payload["error"]["code"], stage="claim")
                    if not await IdempotencyRepository.complete(s, rec_id=rec_id, token=token, status=result.status,
                                                                payload=result.payload, booking_id=None):
                        raise _FencedOut()
                    return result

                if delay_ms:  # failure injection: widen the critical section while holding the row lock
                    await asyncio.sleep(delay_ms / 1000)

                p = body.passenger
                pnr = _new_pnr()
                created_at = (await s.execute(BookingRepository.INSERT, {
                    "id": booking_id, "pnr": pnr, "user_id": user_id, "journey_id": body.journey_id,
                    "seat_id": claimed.id, "name": p.name, "age": p.age, "gender": p.gender,
                    "node_id": self.node, "request_id": request_id,
                })).scalar_one()

                out = BookingOut(
                    booking_id=booking_id, pnr=pnr, status="CONFIRMED", user_id=user_id, journey_id=body.journey_id,
                    seat=claimed.__dict__, passenger_name=p.name, passenger_age=p.age, passenger_gender=p.gender,
                    node_id=self.node, created_at=created_at, train_number=journey["train_number"],
                    train_name=journey["train_name"], journey_date=journey["journey_date"],
                ).model_dump(mode="json")
                out["request_id"] = str(request_id)
                out["coordination"] = lock_mode
                # Booking row, seat claim and the stored idempotent response commit atomically.
                if not await IdempotencyRepository.complete(s, rec_id=rec_id, token=token, status=201,
                                                            payload=out, booking_id=booking_id):
                    raise _FencedOut()
            recorder.record(EventType.BOOKING_COMMITTED, **ev, booking_id=booking_id, outcome="201",
                            seat_id=claimed.id, shard=claimed.shard_id, coordination=lock_mode)
            log.info("booking committed request=%s booking=%s seat=%s shard=%s", request_id, booking_id, claimed.id, claimed.shard_id)
            return BookingResult(201, out)

        except _FencedOut:
            recorder.record(EventType.REQUEST_IN_PROGRESS, **ev, outcome="FENCED")
            return self._err(409, "REQUEST_IN_PROGRESS", "This request was taken over by a retry; fetch its result by retrying.", request_id)
        except IntegrityError as exc:
            # Defence in depth: only reachable if the conditional UPDATE were bypassed. The partial unique
            # index uq_one_confirmed_booking_per_seat rejects a second CONFIRMED booking for a seat.
            recorder.record(EventType.SEAT_UNAVAILABLE, **ev, outcome="409", stage="unique_index", error=type(exc.orig).__name__)
            return await self._complete_only(rec_id, token, self._seat_unavailable(request_id, body.seat_id))

