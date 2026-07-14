"""JSON logging + request-id middleware."""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import get_settings

REQUEST_ID_HEADER = "X-Request-ID"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for attr in ("request_id", "method", "path", "status", "duration_ms",
                     "user_id", "ip"):
            v = getattr(record, attr, None)
            if v is not None:
                payload[attr] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    s = get_settings()
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.setLevel(s.LOG_LEVEL.upper())
    # silencia loggers ruidosos
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Asigna request_id y loggea cada request en JSON."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.log = logging.getLogger("http")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self.log.exception(
                "request_unhandled",
                extra={
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "ip": _client_ip(request),
                },
            )
            raise
        dur_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers[REQUEST_ID_HEADER] = rid
        self.log.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": dur_ms,
                "ip": _client_ip(request),
            },
        )
        return response


def _client_ip(request: Request) -> str | None:
    # respeta X-Forwarded-For (Caddy lo agrega)
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None
