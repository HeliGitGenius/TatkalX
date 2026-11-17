"""Pydantic V2 request/response schemas."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,79}$")


class TrainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    train_number: str
    train_name: str
    source: str
    destination: str
    departure_time: time
    arrival_time: time
    duration_minutes: int
    is_simulated: bool = True


class JourneyOut(BaseModel):
    id: int
    journey_date: date
    status: str
    is_test: bool
    label: str
    train: TrainOut
    total_seats: int
    available_seats: int
    booked_seats: int


class SeatOut(BaseModel):
    id: int
    coach_number: str
    seat_number: int
    berth_type: str
    shard_id: int
    # AVAILABLE | HELD (a booking for it is in flight, Redis lock held) | BOOKED | BLOCKED
    status: str


class SeatMapOut(BaseModel):
    journey: JourneyOut
    coaches: list[str]
    seats: list[SeatOut]
    coordination_mode: str


class PassengerIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    age: int = Field(ge=1, le=120)
    gender: Literal["M", "F", "O"]

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not _NAME_RE.match(v):
            raise ValueError("name may contain letters, spaces, '.', apostrophes and hyphens only")
        return v


class BookingCreate(BaseModel):
    journey_id: int = Field(gt=0)
    seat_id: int | None = Field(default=None, gt=0, description="Specific seat; omit with auto_allocate=true")
    auto_allocate: bool = False
    passenger: PassengerIn

    @model_validator(mode="after")
    def _seat_or_auto(self) -> "BookingCreate":
        if self.auto_allocate == (self.seat_id is not None):
            raise ValueError("provide exactly one of seat_id or auto_allocate=true")
        return self


class SeatRef(BaseModel):
    id: int
    coach_number: str
    seat_number: int
    berth_type: str
    shard_id: int


class BookingOut(BaseModel):
    booking_id: uuid.UUID
    pnr: str
    status: str
    user_id: str
    journey_id: int
    seat: SeatRef
    passenger_name: str
    passenger_age: int
    passenger_gender: str
    node_id: str
    created_at: datetime
    train_number: str | None = None
    train_name: str | None = None
    journey_date: date | None = None
    simulated: bool = True


class AdminResetIn(BaseModel):
    scenario: Literal["one_seat", "limited", "idempotency", "rate_limit"] = "one_seat"
    seats: int = Field(default=1, ge=1, le=500)


class BenchmarkIn(BaseModel):
    scenario: Literal["one_seat", "limited", "idempotency", "rate_limit", "naive_race"] = "one_seat"
    requests: int = Field(default=500, ge=1, le=2000)
    seats: int = Field(default=50, ge=1, le=500)
    allocation: Literal["targeted", "auto"] = "targeted"


class DelayIn(BaseModel):
    delay_ms: int = Field(ge=0, le=5000)
    ttl_s: int = Field(default=120, ge=1, le=3600)


class UnhealthyIn(BaseModel):
    seconds: float = Field(default=15, gt=0, le=600)


class NaiveBookIn(BaseModel):
    seat_id: int
    user_id: str
