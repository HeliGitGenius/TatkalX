"""TatkalX backend node. Run three of these (NODE_ID=A/B/C) behind Nginx."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, public
from app.core.config import get_settings
from app.core.db import dispose_engine, get_engine
from app.core.errors import ApiError, error_body
from app.core.log_config import configure_logging
from app.core.redis_client import close_redis
from app.distributed import failure
from app.monitoring.events import EventType, recorder

configure_logging()
log = logging.getLogger("tatkalx")


@asynccontextmanager
async def lifespan(_: FastAPI):
    st = get_settings()
    get_engine()
    recorder.start()
    recorder.record(EventType.NODE_STARTED, outcome="UP", coordination=st.coordination_mode)
    log.info("node %s started (coordination=%s, env=%s)", st.node_id, st.coordination_mode, st.app_env)
    yield
    recorder.record(EventType.NODE_STOPPING, outcome="GRACEFUL")
    await recorder.stop()
    await close_redis()
    await dispose_engine()


settings = get_settings()
app = FastAPI(
    title="TatkalX — Distributed Ticket Booking API (SIMULATED)",
    version="1.0.0",
    description="Academic distributed-systems demo. All trains and bookings are simulated.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-User-Id", "X-Admin-Token", "Idempotency-Key"],
    expose_headers=["X-Served-By", "X-Request-Id", "Idempotent-Replayed", "Retry-After"],
)


@app.middleware("http")
async def node_context(request: Request, call_next):
    request.state.request_id = uuid.uuid4()
    # Brown-out injection: this node answers 503 (Nginx fails over), except the failure controls.
    if failure.unhealthy_remaining() > 0 and not request.url.path.startswith("/api/admin/failure"):
        response = JSONResponse(error_body("NODE_UNHEALTHY", f"Node {settings.node_id} is unhealthy (injected)"),
                                status_code=503)
    else:
        response = await call_next(request)
    response.headers["X-Served-By"] = settings.node_id
    response.headers["X-Request-Id"] = str(request.state.request_id)
    return response


@app.exception_handler(ApiError)
async def api_error(request: Request, exc: ApiError):
    return JSONResponse(error_body(exc.code, exc.message, node_id=settings.node_id), status_code=exc.status,
                        headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
    return JSONResponse(error_body("VALIDATION_ERROR", "Request validation failed", details=details,
                                   node_id=settings.node_id), status_code=422)


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    recorder.record(EventType.UNEXPECTED_ERROR, outcome="500", path=request.url.path, error=type(exc).__name__)
    return JSONResponse(error_body("INTERNAL_ERROR", "Unexpected server error", node_id=settings.node_id),
                        status_code=500)


app.include_router(public.router, tags=["public"])
app.include_router(admin.router, tags=["admin"])
