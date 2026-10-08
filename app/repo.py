"""All SQL lives here. Every statement is parameterized and scoped by user_id."""
from __future__ import annotations

from typing import Any

import psycopg
from pydantic import ValidationError

from . import crypto
from .models import Settings, SyncRequest


class SyncConflict(Exception):
    """A /sync request referenced items that cannot be saved. Maps to HTTP 422."""

    def __init__(self, ids: list[str], message: str):
        super().__init__(message)
        self.ids = ids
        self.message = message


# ------------------------------------------------------------------------------- reads

def settings_dict(user: dict) -> dict:
    return {
        "padding_min": user["padding_min"],
        "work_start": user["work_start"],
        "work_end": user["work_end"],
        "spread_mode": user["spread_mode"],
        "timezone": user["timezone"],
    }


def fetch_tasks(conn: psycopg.Connection, uid: Any) -> list[dict]:
    return conn.execute(
        "SELECT id, title, due_at, duration_min, splittable, min_chunk_min FROM tasks "
        "WHERE user_id = %s ORDER BY due_at, id",
        (uid,),
    ).fetchall()


def fetch_blocks(conn: psycopg.Connection, uid: Any) -> list[dict]:
    return conn.execute(
        'SELECT id, title, start_at AS "start", end_at AS "end", rrule FROM blocks '
        "WHERE user_id = %s ORDER BY start_at, id",
        (uid,),
    ).fetchall()


def fetch_chunks(conn: psycopg.Connection, uid: Any) -> list[dict]:
    return conn.execute(
        'SELECT id, task_id, start_at AS "start", end_at AS "end", locked FROM scheduled_chunks '
        "WHERE user_id = %s ORDER BY start_at, id",
        (uid,),
    ).fetchall()


def fetch_calendars(conn: psycopg.Connection, uid: Any) -> list[dict]:
    return conn.execute(
        "SELECT google_calendar_id AS id, name, selected, padding_override_min FROM calendars "
        "WHERE user_id = %s ORDER BY name, google_calendar_id",
        (uid,),
    ).fetchall()


def fetch_dismissals(conn: psycopg.Connection, uid: Any) -> list[dict]:
    return conn.execute(
        "SELECT id, calendar_id, google_event_id, scope FROM dismissed_events WHERE user_id = %s "
        "ORDER BY calendar_id, google_event_id",
        (uid,),
    ).fetchall()


def get_refresh_token(conn: psycopg.Connection, uid: Any) -> str | None:
    row = conn.execute("SELECT refresh_token_enc FROM google_tokens WHERE user_id = %s", (uid,)).fetchone()
    if row is None:
        return None
    return crypto.decrypt(row["refresh_token_enc"])


# ------------------------------------------------------------------------ google login

def upsert_google_user(
    conn: psycopg.Connection,
    *,
    google_id: str,
    email: str,
    default_timezone: str,
    refresh_token: str | None,
    calendars: list[dict],
) -> Any:
    with conn.transaction():
        uid = conn.execute(
            """
            INSERT INTO users (google_id, email, timezone) VALUES (%s, %s, %s)
            ON CONFLICT (google_id) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
            """,
            (google_id, email, default_timezone),
        ).fetchone()["id"]
        if refresh_token:
            conn.execute(
                """
                INSERT INTO google_tokens (user_id, refresh_token_enc) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET refresh_token_enc = EXCLUDED.refresh_token_enc, updated_at = now()
                """,
                (uid, crypto.encrypt(refresh_token)),
            )
        sync_calendar_list(conn, uid, calendars)
    return uid


def sync_calendar_list(conn: psycopg.Connection, uid: Any, calendars: list[dict]) -> None:
    """Mirror Google's calendar list. New calendars start unselected; choices are preserved."""
    if not calendars:
        return
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO calendars (user_id, google_calendar_id, name) VALUES (%s, %s, %s)
                ON CONFLICT (user_id, google_calendar_id) DO UPDATE SET name = EXCLUDED.name
                """,
                [(uid, c["id"], c["name"]) for c in calendars],
            )
        conn.execute(
            "DELETE FROM calendars WHERE user_id = %s AND NOT (google_calendar_id = ANY(%s))",
            (uid, [c["id"] for c in calendars]),
        )


# ------------------------------------------------------------------------------ /sync

def _batch(conn: psycopg.Connection, sql: str, rows: list[tuple], ids: list[Any], what: str) -> None:
    """executemany in a savepoint; on failure, find which items are at fault."""
    if not rows:
        return
    errors = (psycopg.errors.IntegrityError, psycopg.errors.DataError)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
    except errors:
        bad: list[str] = []
        for row, item_id in zip(rows, ids):
            try:
                with conn.transaction():
                    conn.execute(sql, row)
            except errors:
                bad.append(str(item_id))
        raise SyncConflict(bad or [str(i) for i in ids], f"Could not save {what}.")


def apply_sync(conn: psycopg.Connection, uid: Any, req: SyncRequest) -> None:
    """Apply a batch of changes in one transaction. Idempotent: upserts by ID, deletes ignore misses."""
    with conn.transaction():
        # --- settings
        if req.settings is not None and req.settings.model_fields_set:
            row = conn.execute(
                "SELECT padding_min, spread_mode, timezone, to_char(work_start, 'HH24:MI') AS work_start, "
                "to_char(work_end, 'HH24:MI') AS work_end FROM users WHERE id = %s FOR UPDATE",
                (uid,),
            ).fetchone()
            patch = {k: v for k, v in req.settings.model_dump().items() if v is not None}
            try:
                merged = Settings(**{**row, **patch})
            except ValidationError as exc:
                raise SyncConflict(["settings"], exc.errors()[0]["msg"]) from exc
            conn.execute(
                "UPDATE users SET padding_min = %s, work_start = %s::time, work_end = %s::time, "
                "spread_mode = %s, timezone = %s WHERE id = %s",
                (merged.padding_min, merged.work_start, merged.work_end, merged.spread_mode, merged.timezone, uid),
            )

        # --- calendar selection / padding overrides (calendars are created by Google sync only)
        unknown: list[str] = []
        for patch_item in req.calendars:
            sets, params = [], []
            if patch_item.selected is not None:
                sets.append("selected = %s")
                params.append(patch_item.selected)
            if "padding_override_min" in patch_item.model_fields_set:
                sets.append("padding_override_min = %s")
                params.append(patch_item.padding_override_min)
            if not sets:
                sets.append("selected = selected")
            cur = conn.execute(
                f"UPDATE calendars SET {', '.join(sets)} WHERE user_id = %s AND google_calendar_id = %s",
                (*params, uid, patch_item.id),
            )
            if cur.rowcount == 0:
                unknown.append(patch_item.id)
        if unknown:
            raise SyncConflict(unknown, "Unknown calendar ids.")

        # --- deletes (children first; deleting a task also removes its chunks)
        for table, changes in (
            ("dismissed_events", req.dismissed_events),
            ("scheduled_chunks", req.chunks),
            ("blocks", req.blocks),
            ("tasks", req.tasks),
        ):
            if changes.delete:
                conn.execute(f"DELETE FROM {table} WHERE user_id = %s AND id = ANY(%s)", (uid, changes.delete))

        # --- upserts (parents first)
        _batch(
            conn,
            """
            INSERT INTO tasks (user_id, id, title, due_at, duration_min, splittable, min_chunk_min)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, id) DO UPDATE SET title = EXCLUDED.title, due_at = EXCLUDED.due_at,
                duration_min = EXCLUDED.duration_min, splittable = EXCLUDED.splittable,
                min_chunk_min = EXCLUDED.min_chunk_min
            """,
            [(uid, t.id, t.title, t.due_at, t.duration_min, t.splittable, t.min_chunk_min) for t in req.tasks.upsert],
            [t.id for t in req.tasks.upsert],
            "tasks",
        )
        _batch(
            conn,
            """
            INSERT INTO blocks (user_id, id, title, start_at, end_at, rrule) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, id) DO UPDATE SET title = EXCLUDED.title, start_at = EXCLUDED.start_at,
                end_at = EXCLUDED.end_at, rrule = EXCLUDED.rrule
            """,
            [(uid, b.id, b.title, b.start, b.end, b.rrule) for b in req.blocks.upsert],
            [b.id for b in req.blocks.upsert],
            "blocks",
        )
        _batch(
            conn,
            """
            INSERT INTO scheduled_chunks (user_id, id, task_id, start_at, end_at, locked)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, id) DO UPDATE SET task_id = EXCLUDED.task_id, start_at = EXCLUDED.start_at,
                end_at = EXCLUDED.end_at, locked = EXCLUDED.locked
            """,
            [(uid, c.id, c.task_id, c.start, c.end, c.locked) for c in req.chunks.upsert],
            [c.id for c in req.chunks.upsert],
            "chunks (does each task_id exist?)",
        )
        _batch(
            conn,
            """
            INSERT INTO dismissed_events (user_id, id, calendar_id, google_event_id, scope)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, id) DO UPDATE SET calendar_id = EXCLUDED.calendar_id,
                google_event_id = EXCLUDED.google_event_id, scope = EXCLUDED.scope
            """,
            [(uid, d.id, d.calendar_id, d.google_event_id, d.scope) for d in req.dismissed_events.upsert],
            [d.id for d in req.dismissed_events.upsert],
            "dismissed events (does each calendar_id exist?)",
        )


def delete_user(conn: psycopg.Connection, uid: Any) -> None:
    """Cascades to sessions, tokens, calendars, tasks, blocks, chunks, and dismissals."""
    conn.execute("DELETE FROM users WHERE id = %s", (uid,))
