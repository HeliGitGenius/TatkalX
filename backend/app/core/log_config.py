"""Structured-ish logging. Passenger names and secrets are never passed to the logger."""
from __future__ import annotations

import logging
import sys

from app.core.config import get_settings


def configure_logging() -> None:
    node = get_settings().node_id
    fmt = f"%(asctime)s node={node} %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stdout, force=True)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
