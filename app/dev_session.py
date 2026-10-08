"""Create (or reuse) a local dev user and print a fresh bearer token.

Lets you exercise the logged-in endpoints before Google OAuth is set up:

    python -m app.dev_session
    curl -H "Authorization: Bearer <token>" http://localhost:8000/state

This is a command-line tool, not an endpoint, and it needs database access to run. It refuses
to run when ENVIRONMENT=production.
"""
from __future__ import annotations

from .auth import create_session
from .config import get_config
from .db import close_pool, connect

DEV_GOOGLE_ID = "dev-local-user"


def main() -> None:
    if get_config().is_production:
        raise SystemExit("Refusing to create a dev session when ENVIRONMENT=production.")
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
