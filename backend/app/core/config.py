"""Centralised configuration. Every value can be overridden with an environment variable."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- identity -------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    node_id: str = Field(default="A", description="Distinct identifier for this backend instance")

    # --- data stores ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://tatkalx:tatkalx_dev_pw@127.0.0.1:5432/tatkalx"
    redis_url: str = "redis://127.0.0.1:6379/0"
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_pool_timeout_s: float = 5.0
    db_statement_timeout_ms: int = 5000
    db_lock_timeout_ms: int = 3000
    redis_socket_timeout_s: float = 0.5

    # --- coordination ---------------------------------------------------------
    # redis_lock: Redis per-seat lock (advisory) + PostgreSQL atomic claim (authoritative)
    # db_only:    PostgreSQL atomic claim only
    coordination_mode: Literal["redis_lock", "db_only"] = "redis_lock"
    lock_ttl_ms: int = 5000
    lock_wait_ms: int = 300
    lock_retry_interval_ms: int = 15
    # fail_open: if Redis is down, continue with DB-only protection (still correct)
    # fail_closed: if Redis is down, reject bookings with 503
    redis_failure_policy: Literal["fail_open", "fail_closed"] = "fail_open"

    # --- idempotency ----------------------------------------------------------
    idempotency_processing_timeout_s: float = 10.0

    # --- rate limiting --------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_window_s: int = 60
    rate_limit_per_user: int = 20
    rate_limit_per_ip: int = 120
    # Test mode raises ONLY the per-IP ceiling (the benchmark sends everything from one IP).
    # Per-user limits, locks, constraints and idempotency all stay on.
    rate_limit_test_mode: bool = False
    rate_limit_test_ip_limit: int = 100_000

    # --- sharding (logical) ---------------------------------------------------
    num_shards: int = 3

    # --- admin / dev ----------------------------------------------------------
    admin_token: str = "change-me-admin-token"
    enable_dev_endpoints: bool = True
    load_balancer_url: str = "http://127.0.0.1:8080"
    reports_dir: str = "../load_testing/reports"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080"

    @field_validator("node_id")
    @classmethod
    def _node_id(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 16:
            raise ValueError("NODE_ID must be 1-16 characters")
        return v

    @property
    def dev_endpoints_allowed(self) -> bool:
        return self.enable_dev_endpoints and self.app_env != "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_ip_limit(self) -> int:
        return self.rate_limit_test_ip_limit if self.rate_limit_test_mode else self.rate_limit_per_ip


@lru_cache
def get_settings() -> Settings:
    return Settings()
