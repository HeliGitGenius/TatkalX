"""Buffered, asynchronous event log writer.

Events are queued in memory and flushed in multi-row INSERTs every 250 ms (or every
500 events). This keeps audit logging off the booking critical path. Trade-off: if a node
crashes, at most ~250 ms of *audit* events may be lost -- never booking data, which is
committed transactionally in bookings / journey_seats / booking_requests.

Never put passenger names or secrets into event metadata.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import insert

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import BookingEvent

log = logging.getLogger("tatkalx.events")


class EventType(StrEnum):
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    RATE_LIMITED = "RATE_LIMITED"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    REQUEST_IN_PROGRESS = "REQUEST_IN_PROGRESS"
    LOCK_ACQUIRED = "LOCK_ACQUIRED"
    LOCK_FAILED = "LOCK_FAILED"
    LOCK_RELEASED = "LOCK_RELEASED"
    LOCK_LEASE_LOST = "LOCK_LEASE_LOST"
    SEAT_CLAIM_ATTEMPTED = "SEAT_CLAIM_ATTEMPTED"
    BOOKING_COMMITTED = "BOOKING_COMMITTED"
    SEAT_UNAVAILABLE = "SEAT_UNAVAILABLE"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"
    NODE_STARTED = "NODE_STARTED"
    NODE_STOPPING = "NODE_STOPPING"
    FAILURE_INJECTED = "FAILURE_INJECTED"


class EventRecorder:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50_000)
        self._task: asyncio.Task | None = None
        self.dropped = 0
        self.written = 0

    def record(
        self,
        event_type: EventType | str,
        *,
        request_id: uuid.UUID | None = None,
        booking_id: uuid.UUID | None = None,
        outcome: str | None = None,
        journey_id: int | None = None,
        **metadata: Any,
    ) -> None:
        row = {
            "node_id": get_settings().node_id,
            "event_type": str(event_type),
            "request_id": request_id,
            "booking_id": booking_id,
            "outcome": outcome,
            "journey_id": journey_id,
            "event_timestamp": datetime.now(timezone.utc),
            "metadata_": metadata or None,
        }
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _flush_once(self) -> int:
        batch: list[dict[str, Any]] = []
        while not self._queue.empty() and len(batch) < 500:
            batch.append(self._queue.get_nowait())
        if not batch:
            return 0
        try:
            async with get_sessionmaker()() as session:
                await session.execute(insert(BookingEvent), batch)
                await session.commit()
            self.written += len(batch)
        except Exception as exc:  # DB down: keep the node alive, count the loss
            self.dropped += len(batch)
            log.warning("event flush failed (%d events dropped): %s", len(batch), type(exc).__name__)
        return len(batch)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            while await self._flush_once() == 500:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="event-writer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        while await self._flush_once():
            pass

    @property
    def backlog(self) -> int:
        return self._queue.qsize()


recorder = EventRecorder()
