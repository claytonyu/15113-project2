"""Create (or reuse) a local dev user and print a fresh bearer token.

Lets you exercise the logged-in endpoints before Google OAuth is set up:

    ALLOW_DEV_SESSION=1 python -m app.dev_session
    curl -H "Authorization: Bearer <token>" http://localhost:8000/state

This is a command-line tool, not an endpoint, and it needs database access to run. It refuses
to run when ENVIRONMENT=production, and unless ALLOW_DEV_SESSION=1 is set for that one command
(do not put it in .env). It prints the database host it writes to, because DATABASE_URL may
point at a database you did not mean to touch.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

from .auth import create_session
from .config import get_config
from .db import close_pool, connect

DEV_GOOGLE_ID = "dev-local-user"


def main() -> None:
    cfg = get_config()
    if cfg.is_production:
        raise SystemExit("Refusing to create a dev session when ENVIRONMENT=production.")
    if os.environ.get("ALLOW_DEV_SESSION") != "1":
        raise SystemExit("Set ALLOW_DEV_SESSION=1 for this command to create a dev session, for example:\n"
                         "  ALLOW_DEV_SESSION=1 python -m app.dev_session")
    host = urlsplit(cfg.database_url or "").hostname or "(DATABASE_URL is not set)"
    print(f"Creating a dev user and session in the database at {host}", file=sys.stderr)
    with connect() as conn:
        uid = conn.execute(
            """
            INSERT INTO users (google_id, email) VALUES (%s, 'dev@localhost')
            ON CONFLICT (google_id) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
            """,
            (DEV_GOOGLE_ID,),
        ).fetchone()["id"]
        token = create_session(conn, uid)
    close_pool()
    print(token)


if __name__ == "__main__":
    main()
