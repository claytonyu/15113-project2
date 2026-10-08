"""FastAPI application.

Run locally:   uvicorn app.main:app --reload
On Render:     uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg
import psycopg_pool
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import crypto, db
from .config import get_config
from .limiter import limiter
from .repo import SyncConflict
from .routes import auth as auth_routes
from .routes import data as data_routes
from .routes import schedule as schedule_routes

log = logging.getLogger("taskplanner")

MAX_BODY_BYTES = 2_000_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    db.close_pool()


app = FastAPI(
    title="TaskPlanner API",
    version="1.0.0",
    description="Backend for TaskPlanner. See SPEC.md for the full design.",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ------------------------------------------------------------------------------ middleware

class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Reject request bodies over a fixed size (the schedule endpoint is open to guests)."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            return await self._reject(send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = b'{"detail":"Request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)
# Added last so it is outermost: even error responses carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_config().frontend_origin],  # strict allowlist; no credentials mode needed
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
    max_age=600,
)


# ---------------------------------------------------------------------------- error handlers

def _offending_ids(body, errors) -> list[str]:
    """Best effort: the `id` of each item in the request body that failed validation."""
    ids: list[str] = []
    for err in errors:
        cur, last_id = body, None
        for part in err.get("loc", ())[1:]:
            try:
                cur = cur[part]
            except (KeyError, IndexError, TypeError):
                break
            if isinstance(cur, dict) and isinstance(cur.get("id"), str):
                last_id = cur["id"]
        if last_id and last_id not in ids:
            ids.append(last_id)
    return ids[:100]


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    content = {"detail": jsonable_encoder(exc.errors()), "ids": _offending_ids(exc.body, exc.errors())}
    return JSONResponse(status_code=422, content=content)


@app.exception_handler(SyncConflict)
async def on_sync_conflict(request: Request, exc: SyncConflict):
    return JSONResponse(status_code=422, content={"detail": exc.message, "ids": exc.ids})


@app.exception_handler(db.DatabaseNotConfigured)
async def on_db_not_configured(request: Request, exc: db.DatabaseNotConfigured):
    return JSONResponse(status_code=503, content={"detail": "Database is not configured on this server"})


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(psycopg_pool.PoolTimeout)
async def on_db_unavailable(request: Request, exc: Exception):
    log.warning("database unavailable: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable, please retry"}, headers={"Retry-After": "3"})


@app.exception_handler(crypto.CryptoNotConfigured)
async def on_crypto_not_configured(request: Request, exc: crypto.CryptoNotConfigured):
    return JSONResponse(status_code=503, content={"detail": "Server encryption is not configured"})


app.include_router(auth_routes.router)
app.include_router(data_routes.router)
app.include_router(schedule_routes.router)
