"""Glue between the database and Google that several endpoints share."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
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


def _retry_on_rejected_token(uid: Any, access: str, run: Callable[[str], tuple[Any, str | None]]):
    """run(access) -> (result, error_code). If Google rejects the (cached) access token, forget it,
    get a fresh one with the refresh token, and try once more. Only a rejected refresh token (or a
    second rejection) is reported as reauth_required, so the user is not sent to log in again for
    nothing."""
    result, error = run(access)
    if error != google.ACCESS_TOKEN_REJECTED:
        return result, error
    google.forget_access_token(str(uid))
    fresh, error = google_access(uid)
    if fresh is None:
        return result, error
    result, error = run(fresh)
    return result, ("reauth_required" if error == google.ACCESS_TOKEN_REJECTED else error)


def list_user_calendars(uid: Any, access: str) -> tuple[list[dict] | None, str | None]:
    """The user's Google calendar list, as (calendars, None) or (None, error_code)."""

    def run(token: str):
        try:
            return google.list_calendars(token), None
        except google.GoogleError as exc:
            return None, exc.code

    return _retry_on_rejected_token(uid, access, run)


def fetch_user_events(
    uid: Any, access: str, calendar_ids: list[str], time_min: datetime, time_max: datetime, tz: ZoneInfo
) -> tuple[list[dict], str | None]:
    """Events from the given calendars with the user's dismissals applied."""
    events, error = _retry_on_rejected_token(
        uid, access, lambda token: google.fetch_events(token, calendar_ids, time_min, time_max, tz)
    )
    with connect() as conn:
        dismissals = repo.fetch_dismissals(conn, uid)
    return google.mark_dismissed(events, dismissals), error
