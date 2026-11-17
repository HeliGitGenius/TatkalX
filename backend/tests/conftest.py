"""Test fixtures. Tests run against a real PostgreSQL + Redis (the things being tested).

Set TEST_DATABASE_URL / TEST_REDIS_URL to point elsewhere.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://tatkalx:tatkalx_dev_pw@127.0.0.1:5432/tatkalx_test")
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ["NODE_ID"] = "T"
os.environ["APP_ENV"] = "test"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["RATE_LIMIT_TEST_MODE"] = "true"
os.environ["DB_POOL_SIZE"] = "30"

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import dispose_engine, get_sessionmaker  # noqa: E402
from app.core.redis_client import close_redis, get_redis  # noqa: E402
from app.main import app  # noqa: E402
from app.monitoring.events import recorder  # noqa: E402
from app.services.seed_service import reset_test_journey  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], cwd=BACKEND, check=True,
                   capture_output=True, env=os.environ.copy())
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, check=True,
                   capture_output=True, env=os.environ.copy())
    yield


@pytest.fixture(scope="session", autouse=True)
async def lifecycle(migrated_db):
    await get_redis().flushdb()
    recorder.start()
    yield
    await recorder.stop()
    await close_redis()
    await dispose_engine()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


@pytest.fixture
def settings():
    s = get_settings()
    snapshot = s.model_dump()
    yield s
    for k, v in snapshot.items():
        setattr(s, k, v)


async def make_journey(seats: int) -> tuple[int, list[int]]:
    async with get_sessionmaker()() as s, s.begin():
        out = await reset_test_journey(s, seats, label=f"pytest-{os.urandom(3).hex()}")
    return out["journey_id"], out["seat_ids"]


def booking_body(journey_id: int, seat_id: int | None = None, name: str = "Test Passenger") -> dict:
    body = {"journey_id": journey_id, "passenger": {"name": name, "age": 30, "gender": "F"}}
    if seat_id is None:
        body["auto_allocate"] = True
    else:
        body["seat_id"] = seat_id
    return body
