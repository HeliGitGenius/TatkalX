"""Data-access layer. All SQL is parameterised (bound parameters, never string formatting)."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------- seats
@dataclass
class ClaimedSeat:
    id: int
    coach_number: str
    seat_number: int
    berth_type: str
    shard_id: int


class SeatRepository:
    # THE critical statement: the WHERE status='AVAILABLE' predicate makes check-and-set one
    # atomic step. Under READ COMMITTED, a concurrent UPDATE of the same row blocks on the row
    # lock, then re-evaluates the WHERE clause against the committed row and matches 0 rows.
    CLAIM_SPECIFIC = text(
        """
        UPDATE journey_seats
           SET status = 'BOOKED', booking_id = :booking_id, version = version + 1, updated_at = now()
         WHERE id = :seat_id AND journey_id = :journey_id AND status = 'AVAILABLE'
        RETURNING id, coach_number, seat_number, berth_type, shard_id
        """
    )

    # Automatic allocation: pick the lowest free seat that nobody else is currently claiming.
    # FOR UPDATE SKIP LOCKED lets concurrent transactions take *different* free seats instead
    # of queueing on the same one. The outer predicate re-checks status for safety.
    CLAIM_AUTO = text(
        """
        UPDATE journey_seats
           SET status = 'BOOKED', booking_id = :booking_id, version = version + 1, updated_at = now()
         WHERE id = (
                SELECT id FROM journey_seats
                 WHERE journey_id = :journey_id AND status = 'AVAILABLE'
                 ORDER BY coach_number, seat_number
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1)
           AND status = 'AVAILABLE'
        RETURNING id, coach_number, seat_number, berth_type, shard_id
        """
    )

    @staticmethod
    async def claim_specific(s: AsyncSession, journey_id: int, seat_id: int, booking_id: uuid.UUID) -> ClaimedSeat | None:
        rows = (await s.execute(SeatRepository.CLAIM_SPECIFIC, {"seat_id": seat_id, "journey_id": journey_id, "booking_id": booking_id})).all()
        if len(rows) != 1:  # must be exactly one row
            return None
        return ClaimedSeat(*rows[0])

    @staticmethod
    async def claim_auto(s: AsyncSession, journey_id: int, booking_id: uuid.UUID) -> ClaimedSeat | None:
        rows = (await s.execute(SeatRepository.CLAIM_AUTO, {"journey_id": journey_id, "booking_id": booking_id})).all()
        if len(rows) != 1:
            return None
        return ClaimedSeat(*rows[0])

    @staticmethod
    async def get_seat(s: AsyncSession, seat_id: int) -> dict | None:
        row = (
            await s.execute(
                text("SELECT id, journey_id, coach_number, seat_number, berth_type, shard_id, status FROM journey_seats WHERE id = :id"),
                {"id": seat_id},
            )
        ).mappings().first()
        return dict(row) if row else None

    @staticmethod
    async def seats_for_journey(s: AsyncSession, journey_id: int) -> list[dict]:
        rows = (
            await s.execute(
                text(
                    "SELECT id, coach_number, seat_number, berth_type, shard_id, status FROM journey_seats "
                    "WHERE journey_id = :j ORDER BY coach_number, seat_number"
                ),
                {"j": journey_id},
            )
        ).mappings().all()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- journeys
class JourneyRepository:
    _BASE = """
        SELECT j.id, j.journey_date, j.status, j.is_test, j.label,
               t.id AS t_id, t.train_number, t.train_name, t.source, t.destination,
               t.departure_time, t.arrival_time, t.duration_minutes, t.is_simulated,
               COUNT(s.id) AS total_seats,
               COUNT(s.id) FILTER (WHERE s.status = 'AVAILABLE') AS available_seats,
               COUNT(s.id) FILTER (WHERE s.status = 'BOOKED') AS booked_seats
          FROM journeys j
          JOIN trains t ON t.id = j.train_id
          LEFT JOIN journey_seats s ON s.journey_id = j.id
    """

    @staticmethod
    def _shape(r: Any) -> dict:
        return {
            "id": r["id"],
            "journey_date": r["journey_date"],
            "status": r["status"],
            "is_test": r["is_test"],
            "label": r["label"],
            "total_seats": r["total_seats"],
            "available_seats": r["available_seats"],
            "booked_seats": r["booked_seats"],
            "train": {
                "id": r["t_id"],
                "train_number": r["train_number"],
                "train_name": r["train_name"],
                "source": r["source"],
                "destination": r["destination"],
                "departure_time": r["departure_time"],
                "arrival_time": r["arrival_time"],
                "duration_minutes": r["duration_minutes"],
                "is_simulated": r["is_simulated"],
            },
        }

    @classmethod
    async def search(
        cls, s: AsyncSession, source: str | None, destination: str | None, on: date | None, include_test: bool
    ) -> list[dict]:
        where = ["(:inc_test OR j.is_test = false)"]
        params: dict[str, Any] = {"inc_test": include_test}
        if source:
            where.append("t.source ILIKE :src")
            params["src"] = f"%{source}%"
        if destination:
            where.append("t.destination ILIKE :dst")
            params["dst"] = f"%{destination}%"
        if on:
            where.append("j.journey_date = :on")
            params["on"] = on
        sql = cls._BASE + " WHERE " + " AND ".join(where) + " GROUP BY j.id, t.id ORDER BY j.journey_date, t.departure_time LIMIT 200"
        rows = (await s.execute(text(sql), params)).mappings().all()
        return [cls._shape(r) for r in rows]

    @staticmethod
    async def get_basic(s: AsyncSession, journey_id: int) -> dict | None:
        """Light lookup for the booking hot path (no seat aggregation)."""
        r = (await s.execute(text(
            "SELECT j.id, j.status, j.journey_date, t.train_number, t.train_name FROM journeys j "
            "JOIN trains t ON t.id = j.train_id WHERE j.id = :id"), {"id": journey_id})).mappings().first()
        return dict(r) if r else None

    @classmethod
    async def get(cls, s: AsyncSession, journey_id: int) -> dict | None:
        sql = cls._BASE + " WHERE j.id = :id GROUP BY j.id, t.id"
        r = (await s.execute(text(sql), {"id": journey_id})).mappings().first()
        return cls._shape(r) if r else None


# --------------------------------------------------------------------------- bookings
class BookingRepository:
    INSERT = text(
        """
        INSERT INTO bookings (id, pnr, user_id, journey_id, journey_seat_id, passenger_name,
                              passenger_age, passenger_gender, status, node_id, request_id)
        VALUES (:id, :pnr, :user_id, :journey_id, :seat_id, :name, :age, :gender, 'CONFIRMED', :node_id, :request_id)
        RETURNING created_at
        """
    )

    _SELECT = """
        SELECT b.id AS booking_id, b.pnr, b.status, b.user_id, b.journey_id, b.passenger_name,
               b.passenger_age, b.passenger_gender, b.node_id, b.created_at,
               s.id AS seat_id, s.coach_number, s.seat_number, s.berth_type, s.shard_id,
               t.train_number, t.train_name, j.journey_date
          FROM bookings b
          JOIN journey_seats s ON s.id = b.journey_seat_id
          JOIN journeys j ON j.id = b.journey_id
          JOIN trains t ON t.id = j.train_id
    """

    @staticmethod
    def shape(r: Any) -> dict:
        return {
            "booking_id": r["booking_id"],
            "pnr": r["pnr"],
            "status": r["status"],
            "user_id": r["user_id"],
            "journey_id": r["journey_id"],
            "passenger_name": r["passenger_name"],
            "passenger_age": r["passenger_age"],
            "passenger_gender": r["passenger_gender"],
            "node_id": r["node_id"],
            "created_at": r["created_at"],
            "seat": {
                "id": r["seat_id"],
                "coach_number": r["coach_number"],
                "seat_number": r["seat_number"],
                "berth_type": r["berth_type"],
                "shard_id": r["shard_id"],
            },
            "train_number": r["train_number"],
            "train_name": r["train_name"],
            "journey_date": r["journey_date"],
            "simulated": True,
        }

    @classmethod
    async def get(cls, s: AsyncSession, booking_id: uuid.UUID) -> dict | None:
        r = (await s.execute(text(cls._SELECT + " WHERE b.id = :id"), {"id": booking_id})).mappings().first()
        return cls.shape(r) if r else None

    @classmethod
    async def for_user(cls, s: AsyncSession, user_id: str, limit: int = 100) -> list[dict]:
        rows = (
            await s.execute(text(cls._SELECT + " WHERE b.user_id = :u ORDER BY b.created_at DESC LIMIT :lim"), {"u": user_id, "lim": limit})
        ).mappings().all()
        return [cls.shape(r) for r in rows]


# --------------------------------------------------------------------------- idempotency ledger
@dataclass
class IdemRecord:
    id: uuid.UUID
    request_fingerprint: str
    state: str
    attempt_token: uuid.UUID
    response_status: int | None
    response_payload: dict | None
    stale: bool


class IdempotencyRepository:
    @staticmethod
    async def reserve(
        s: AsyncSession, *, user_id: str, key: str, fingerprint: str, token: uuid.UUID, node_id: str
    ) -> uuid.UUID | None:
        """Insert a PROCESSING row. Returns its id, or None if (user, key) already exists.
        Concurrent inserts of the same key serialise on the unique index; exactly one wins."""
        row = (
            await s.execute(
                text(
                    """
                    INSERT INTO booking_requests (id, user_id, idempotency_key, request_fingerprint, state, attempt_token, node_id)
                    VALUES (:id, :u, :k, :fp, 'PROCESSING', :tok, :node)
                    ON CONFLICT (user_id, idempotency_key) DO NOTHING
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "u": user_id, "k": key, "fp": fingerprint, "tok": token, "node": node_id},
            )
        ).first()
        return row[0] if row else None

    @staticmethod
    async def get(s: AsyncSession, user_id: str, key: str, stale_after_s: float) -> IdemRecord | None:
        r = (
            await s.execute(
                text(
                    """
                    SELECT id, request_fingerprint, state, attempt_token, response_status, response_payload,
                           (state = 'PROCESSING' AND updated_at < now() - make_interval(secs => :stale)) AS stale
                      FROM booking_requests WHERE user_id = :u AND idempotency_key = :k
                    """
                ),
                {"u": user_id, "k": key, "stale": stale_after_s},
            )
        ).first()
        return IdemRecord(*r) if r else None

    @staticmethod
    async def take_over_stale(s: AsyncSession, rec_id: uuid.UUID, new_token: uuid.UUID, node_id: str, stale_after_s: float) -> bool:
        """A PROCESSING record whose owner died can be re-owned. The token swap fences the old owner:
        its final 'complete' UPDATE is conditional on its own token and will match 0 rows."""
        res = await s.execute(
            text(
                """
                UPDATE booking_requests SET attempt_token = :tok, node_id = :node, updated_at = now()
                 WHERE id = :id AND state = 'PROCESSING' AND updated_at < now() - make_interval(secs => :stale)
                """
            ),
            {"tok": new_token, "node": node_id, "id": rec_id, "stale": stale_after_s},
        )
        return res.rowcount == 1

    @staticmethod
    async def complete(
        s: AsyncSession, *, rec_id: uuid.UUID, token: uuid.UUID, status: int, payload: dict, booking_id: uuid.UUID | None
    ) -> bool:
        res = await s.execute(
            text(
                """
                UPDATE booking_requests
                   SET state = 'COMPLETED', response_status = :st, response_payload = CAST(:payload AS jsonb),
                       booking_id = :bid, updated_at = now()
                 WHERE id = :id AND attempt_token = :tok AND state = 'PROCESSING'
                """
            ),
            {"st": status, "payload": json.dumps(payload, default=str), "bid": booking_id, "id": rec_id, "tok": token},
        )
        return res.rowcount == 1

    @staticmethod
    async def release(s: AsyncSession, rec_id: uuid.UUID, token: uuid.UUID) -> None:
        """On an unexpected (infrastructure) error, drop our PROCESSING reservation so a client
        retry can be processed again instead of waiting for the stale timeout."""
        await s.execute(
            text("DELETE FROM booking_requests WHERE id = :id AND attempt_token = :tok AND state = 'PROCESSING'"),
            {"id": rec_id, "tok": token},
        )
