"""HTTP diagnostics and bounded probes for the supported local runtime."""

import asyncio
import logging
import time
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse
import httpx
from psycopg import Error as PostgresError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, func
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException

from customer_support_chat.app.business.database import local_value

logger = logging.getLogger("uvicorn.error.travel")
PROBE_TIMEOUT = 2


def error_response(request: Request, status: int, code: str, detail: str, **context):
    request.state.error_type = code
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "request_id": str(request.state.request_id), **context},
    )


async def http_error(request: Request, error: HTTPException):
    response = error_response(request, error.status_code, f"http_{error.status_code}", error.detail)
    if error.headers:
        response.headers.update(error.headers)
    return response


async def observe_request(request: Request, call_next):
    request.state.request_id = uuid4()
    started = time.monotonic()
    error_type = "-"
    try:
        response = await call_next(request)
    except (PostgresError, DBAPIError, PoolTimeout) as error:
        error_type = type(error).__name__
        response = error_response(request, 503, "database_unavailable", "Database unavailable")
    except Exception as error:
        error_type = type(error).__name__
        response = error_response(request, 500, "internal_error", "Internal server error")

    response.headers["X-Request-ID"] = str(request.state.request_id)
    if request.url.path.startswith(("/api/", "/health/")):
        response.headers["Cache-Control"] = "no-store"
    logger.log(
        logging.ERROR if response.status_code >= 500 else logging.INFO,
        "module=web route=%s method=%s status=%s request_id=%s elapsed_ms=%d error_type=%s",
        getattr(request.scope.get("route"), "name", "unmatched"),
        request.method, response.status_code, request.state.request_id,
        (time.monotonic() - started) * 1000, error_type if error_type != "-" else getattr(request.state, "error_type", "-"),
    )
    return response


async def postgres_schema_ready(engine, relation: str) -> bool:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT), engine.connect() as connection:
            resolved = await connection.scalar(select(func.to_regclass(relation)))
            return resolved is not None
    except (TimeoutError, DBAPIError, PoolTimeout):
        return False


async def redis_ready() -> bool:
    try:
        port = int(local_value("REDIS_PORT", "6379"))
        async with asyncio.timeout(PROBE_TIMEOUT), Redis(
            host="127.0.0.1", port=port, socket_connect_timeout=PROBE_TIMEOUT, socket_timeout=PROBE_TIMEOUT,
        ) as client:
            return bool(await client.ping())
    except (TimeoutError, RedisError, OSError, ValueError):
        return False


async def qdrant_ready() -> bool:
    try:
        port = int(local_value("QDRANT_PORT", "6333"))
        async with asyncio.timeout(PROBE_TIMEOUT), httpx.AsyncClient(timeout=PROBE_TIMEOUT, trust_env=False) as client:
            return (await client.get(f"http://127.0.0.1:{port}/readyz")).is_success
    except (TimeoutError, httpx.HTTPError, ValueError):
        return False
