"""Domain errors mapped to stable JSON error codes."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_body(code: str, message: str, **extra) -> dict:
    return {"error": {"code": code, "message": message}, **extra}
