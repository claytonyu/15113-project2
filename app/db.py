"""Postgres connection pool (psycopg 3).

Notes for Neon:
- `prepare_threshold=None` disables server-side prepared statements, which break behind
  Neon's pooled (pgbouncer) endpoint.
- Neon suspends idle compute, so idle connections are recycled quickly and every checkout
  is health-checked. The first query after a long idle may take a second or two.
- Connections use autocommit; multi-statement writes use explicit `conn.transaction()`.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_config


class DatabaseNotConfigured(RuntimeError):
    pass


_pool: ConnectionPool | None = None
_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            url = get_config().database_url
            if not url:
                raise DatabaseNotConfigured("DATABASE_URL is not set")
            pool = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=10,
                max_idle=240,
                max_lifetime=1800,
                timeout=30,
                open=False,
                check=ConnectionPool.check_connection,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": None,
                    "row_factory": dict_row,
                    "connect_timeout": 20,
                },
            )
            pool.open(wait=False)
            _pool = pool
        return _pool


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None
