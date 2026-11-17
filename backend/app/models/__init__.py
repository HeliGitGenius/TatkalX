"""SQLAlchemy ORM models.

Ownership rules (avoid duplicated ownership of seat state):
  * journey_seats.status is the ONLY place a seat's availability lives.
  * bookings.status says whether the booking itself is active (CONFIRMED) or CANCELLED.
  * journey_seats.booking_id points at the booking currently holding the seat; a CHECK
    constraint ties it to status so the two can never disagree inside one row.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Train(Base):
    __tablename__ = "trains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    train_number: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    train_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    destination: Mapped[str] = mapped_column(String(80), nullable=False)
    departure_time: Mapped[time] = mapped_column(Time, nullable=False)
    arrival_time: Mapped[time] = mapped_column(Time, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Journey(Base):
    __tablename__ = "journeys"
    __table_args__ = (
        UniqueConstraint("train_id", "journey_date", "is_test", "label", name="uq_journey_train_date"),
        CheckConstraint("status IN ('SCHEDULED','CLOSED','CANCELLED')", name="ck_journey_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id", ondelete="CASCADE"), nullable=False, index=True)
    journey_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="SCHEDULED")
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    label: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JourneySeat(Base):
    __tablename__ = "journey_seats"
    __table_args__ = (
        UniqueConstraint("journey_id", "coach_number", "seat_number", name="uq_seat_per_journey"),
        CheckConstraint("status IN ('AVAILABLE','BOOKED','BLOCKED')", name="ck_seat_status"),
        # A seat is BOOKED  <=>  it references a booking.
        CheckConstraint("(status = 'BOOKED') = (booking_id IS NOT NULL)", name="ck_seat_booking_consistency"),
        Index("ix_seats_journey_status", "journey_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    journey_id: Mapped[int] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False)
    coach_number: Mapped[str] = mapped_column(String(4), nullable=False)
    seat_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    berth_type: Mapped[str] = mapped_column(String(4), nullable=False)
    shard_id: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="AVAILABLE")
    # FK added in the migration as DEFERRABLE INITIALLY DEFERRED (circular reference).
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("status IN ('CONFIRMED','CANCELLED')", name="ck_booking_status"),
        CheckConstraint("passenger_age BETWEEN 1 AND 120", name="ck_passenger_age"),
        # Second, independent line of defence: one CONFIRMED booking per seat, ever.
        Index(
            "uq_one_confirmed_booking_per_seat",
            "journey_seat_id",
            unique=True,
            postgresql_where=text("status = 'CONFIRMED'"),
        ),
        Index("ix_bookings_user", "user_id", "created_at"),
        Index("ix_bookings_journey", "journey_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    pnr: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    journey_id: Mapped[int] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False)
    journey_seat_id: Mapped[int] = mapped_column(
        ForeignKey("journey_seats.id", ondelete="CASCADE"), nullable=False
    )
    passenger_name: Mapped[str] = mapped_column(String(80), nullable=False)
    passenger_age: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    passenger_gender: Mapped[str] = mapped_column(String(1), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="CONFIRMED")
    node_id: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BookingRequest(Base):
    """Idempotency ledger: one row per (user, idempotency key)."""

    __tablename__ = "booking_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_idempotency_key_per_user"),
        CheckConstraint("state IN ('PROCESSING','COMPLETED')", name="ck_request_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False, server_default="PROCESSING")
    attempt_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )
    response_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    node_id: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BookingEvent(Base):
    __tablename__ = "booking_events"
    __table_args__ = (
        Index("ix_events_ts", "event_timestamp"),
        Index("ix_events_type_node", "event_type", "node_id"),
        Index("ix_events_request", "request_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    node_id: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    journey_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class NaiveDemoSeat(Base):
    """Deliberately UNPROTECTED table used only by the 'naive_race' demonstration.

    It has no conditional update and no unique constraint so the class can SEE what a
    check-then-write race does. Real bookings never touch this table.
    """

    __tablename__ = "naive_demo_seats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="AVAILABLE")


class NaiveDemoBooking(Base):
    __tablename__ = "naive_demo_bookings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = [
    "Base",
    "Train",
    "Journey",
    "JourneySeat",
    "Booking",
    "BookingRequest",
    "BookingEvent",
    "BenchmarkRun",
    "NaiveDemoSeat",
    "NaiveDemoBooking",
]
