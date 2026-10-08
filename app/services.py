"""Glue between the database and Google that several endpoints share."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import crypto, google, repo
from .db import connect


def google_access(uid: Any) -> tuple[str | None, str | None]:
    """(access_token, None) on success, or (None, error_code).

    Error codes: not_configured, not_connected, reauth_required, google_unavailable.
    """
    try:
        with connect() as conn:
            refresh = repo.get_refresh_token(conn, uid)
            has_row = conn.execute("SELECT 1 FROM google_tokens WHERE user_id = %s", (uid,)).fetchone()
    except crypto.CryptoNotConfigured:
        return None, "not_configured"
    if not has_row:
        return None, "not_connected"
    if refresh is None:
        return None, "reauth_required"
    try:
        return google.access_token_for(str(uid), refresh), None
    except google.GoogleError as exc:
        return None, exc.code


def fetch_user_events(
    uid: Any, access: str, calendar_ids: list[str], time_min: datetime, time_max: datetime, tz: ZoneInfo
) -> tuple[list[dict], str | None]:
    """Events from the given calendars with the user's dismissals applied."""
    events, error = google.fetch_events(access, calendar_ids, time_min, time_max, tz)
    with connect() as conn:
        dismissals = repo.fetch_dismissals(conn, uid)
    return google.mark_dismissed(events, dismissals), error
