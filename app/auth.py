"""Bearer-token sessions.

The raw token is shown once (in the post-login redirect). Only its SHA-256 hash is stored, so a
database leak does not leak usable sessions. Tokens expire after 30 days and logout deletes
the row.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from fastapi import Header, HTTPException

from .db import connect

SESSION_TTL = timedelta(days=30)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: psycopg.Connection, user_id: Any) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")  # opportunistic cleanup
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
        (hash_token(token), user_id, datetime.now(timezone.utc) + SESSION_TTL),
    )
    return token


def delete_session(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),))


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(401, "Invalid Authorization header", headers={"WWW-Authenticate": "Bearer"})
    return value.strip()


def _lookup(token: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.email, u.timezone, u.padding_min, u.spread_mode,
                   to_char(u.work_start, 'HH24:MI') AS work_start,
                   to_char(u.work_end, 'HH24:MI') AS work_end
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > now()
            """,
            (hash_token(token),),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Session expired or invalid", headers={"WWW-Authenticate": "Bearer"})
    return row


def require_user(authorization: str | None = Header(default=None)) -> dict:
    """Dependency: the logged-in user's row, or 401."""
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    user = _lookup(token)
    user["token"] = token
    return user


def optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    """Dependency: None for guests (no header); 401 if a token is sent but invalid."""
    token = _bearer(authorization)
    if token is None:
        return None
    user = _lookup(token)
    user["token"] = token
    return user
