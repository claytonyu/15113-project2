"""Apply schema.sql to the database in DATABASE_URL. Safe to re-run.

    python -m app.db_init
"""
from __future__ import annotations

from pathlib import Path

import psycopg

from .config import get_config

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def main() -> None:
    url = get_config().database_url
    if not url:
        raise SystemExit("DATABASE_URL is not set (put it in .env).")
    with psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=30) as conn:
        conn.execute(SCHEMA_PATH.read_text())
    print("Schema applied.")


if __name__ == "__main__":
    main()
