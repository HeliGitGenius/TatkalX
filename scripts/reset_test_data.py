"""Create a fresh TEST journey with N seats (deletes previous TEST journeys).

Usage:  python scripts/reset_test_data.py --seats 1
"""
import argparse
import asyncio
import json

import _bootstrap  # noqa: F401
from app.core.db import dispose_engine, get_sessionmaker
from app.services.seed_service import reset_test_journey


async def main(seats: int) -> None:
    async with get_sessionmaker()() as s, s.begin():
        out = await reset_test_journey(s, seats, label="cli")
    await dispose_engine()
    out["seat_ids"] = out["seat_ids"][:10] + (["..."] if len(out["seat_ids"]) > 10 else [])
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seats", type=int, default=1)
    asyncio.run(main(ap.parse_args().seats))
