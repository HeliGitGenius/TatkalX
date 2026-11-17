"""Seed SIMULATED trains, 7 days of journeys and some pre-booked seats.

Usage:  python scripts/seed_data.py [--force]
"""
import argparse
import asyncio
import json

import _bootstrap  # noqa: F401
from app.core.db import dispose_engine, get_sessionmaker
from app.services.seed_service import seed


async def main(force: bool) -> None:
    async with get_sessionmaker()() as s, s.begin():
        out = await seed(s, force=force)
    await dispose_engine()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="wipe ALL data and reseed")
    asyncio.run(main(ap.parse_args().force))
