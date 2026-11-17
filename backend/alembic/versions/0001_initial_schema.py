"""Initial TatkalX schema with double-booking integrity constraints.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-15
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trains",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("train_number", sa.String(10), nullable=False, unique=True),
        sa.Column("train_name", sa.String(120), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("destination", sa.String(80), nullable=False),
        sa.Column("departure_time", sa.Time, nullable=False),
        sa.Column("arrival_time", sa.Time, nullable=False),
        sa.Column("duration_minutes", sa.Integer, nullable=False),
        sa.Column("is_simulated", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )

    op.create_table(
        "journeys",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("train_id", sa.Integer, sa.ForeignKey("trains.id", ondelete="CASCADE"), nullable=False),
        sa.Column("journey_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="SCHEDULED"),
        sa.Column("is_test", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("label", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("train_id", "journey_date", "is_test", "label", name="uq_journey_train_date"),
        sa.CheckConstraint("status IN ('SCHEDULED','CLOSED','CANCELLED')", name="ck_journey_status"),
    )
    op.create_index("ix_journeys_train_id", "journeys", ["train_id"])

    op.create_table(
        "journey_seats",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("journey_id", sa.Integer, sa.ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False),
        sa.Column("coach_number", sa.String(4), nullable=False),
        sa.Column("seat_number", sa.SmallInteger, nullable=False),
        sa.Column("berth_type", sa.String(4), nullable=False),
        sa.Column("shard_id", sa.SmallInteger, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="AVAILABLE"),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # (1) A seat exists once per journey -> stable identifier per journey.
        sa.UniqueConstraint("journey_id", "coach_number", "seat_number", name="uq_seat_per_journey"),
        # (2) A seat row can reference at most one booking, and a booking at most one seat row.
        sa.UniqueConstraint("booking_id", name="uq_seat_booking_id"),
        sa.CheckConstraint("status IN ('AVAILABLE','BOOKED','BLOCKED')", name="ck_seat_status"),
        # (3) BOOKED <=> booking_id present. Seat state and its owner cannot drift apart.
        sa.CheckConstraint("(status = 'BOOKED') = (booking_id IS NOT NULL)", name="ck_seat_booking_consistency"),
    )
    op.create_index("ix_seats_journey_status", "journey_seats", ["journey_id", "status"])

    op.create_table(
        "bookings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pnr", sa.String(12), nullable=False, unique=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("journey_id", sa.Integer, sa.ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "journey_seat_id", sa.BigInteger, sa.ForeignKey("journey_seats.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("passenger_name", sa.String(80), nullable=False),
        sa.Column("passenger_age", sa.SmallInteger, nullable=False),
        sa.Column("passenger_gender", sa.String(1), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="CONFIRMED"),
        sa.Column("node_id", sa.String(16), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('CONFIRMED','CANCELLED')", name="ck_booking_status"),
        sa.CheckConstraint("passenger_age BETWEEN 1 AND 120", name="ck_passenger_age"),
    )
    # (4) Independent safety net: at most ONE confirmed booking per journey seat.
    op.create_index(
        "uq_one_confirmed_booking_per_seat",
        "bookings",
        ["journey_seat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'CONFIRMED'"),
    )
    op.create_index("ix_bookings_user", "bookings", ["user_id", "created_at"])
    op.create_index("ix_bookings_journey", "bookings", ["journey_id", "status"])

    # Circular reference seat -> booking. DEFERRABLE INITIALLY DEFERRED lets the booking
    # transaction claim the seat first (taking the row lock) and insert the booking second;
    # the FK is checked at COMMIT.
    op.create_foreign_key(
        "fk_seat_booking",
        "journey_seats",
        "bookings",
        ["booking_id"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "booking_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(12), nullable=False, server_default="PROCESSING"),
        sa.Column("attempt_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("response_status", sa.SmallInteger, nullable=True),
        sa.Column("response_payload", postgresql.JSONB, nullable=True),
        sa.Column("node_id", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # (5) One idempotency record per (user, key) -> retries cannot create a 2nd booking.
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_idempotency_key_per_user"),
        sa.CheckConstraint("state IN ('PROCESSING','COMPLETED')", name="ck_request_state"),
    )

    op.create_table(
        "booking_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("node_id", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("journey_id", sa.Integer, nullable=True),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
    )
    op.create_index("ix_events_ts", "booking_events", ["event_timestamp"])
    op.create_index("ix_events_type_node", "booking_events", ["event_type", "node_id"])
    op.create_index("ix_events_request", "booking_events", ["request_id"])

    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("scenario", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", postgresql.JSONB, nullable=True),
    )

    # Deliberately unprotected tables for the naive race-condition demonstration only.
    op.create_table(
        "naive_demo_seats",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="AVAILABLE"),
    )
    op.create_table(
        "naive_demo_bookings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("seat_id", sa.Integer, nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("naive_demo_bookings")
    op.drop_table("naive_demo_seats")
    op.drop_table("benchmark_runs")
    op.drop_table("booking_events")
    op.drop_table("booking_requests")
    op.drop_constraint("fk_seat_booking", "journey_seats", type_="foreignkey")
    op.drop_table("bookings")
    op.drop_table("journey_seats")
    op.drop_table("journeys")
    op.drop_table("trains")
