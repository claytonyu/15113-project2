"""Endpoints 4, 5, 7: load state, batch-save changes, delete account."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Response

from .. import auth, crypto, google, repo, services
from ..db import connect
from ..models import StateResponse, SyncRequest, SyncResponse

router = APIRouter(tags=["data"])

GOOGLE_LOAD_WINDOW = timedelta(days=60)


@router.get("/state", response_model=StateResponse, summary="Everything the app needs on load")
def get_state(sync_google: bool = True, user: dict = Depends(auth.require_user)):
    uid = user["id"]
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(user["timezone"])
    google_error: str | None = None
    access: str | None = None

    if sync_google:
        access, google_error = services.google_access(uid)
        if access:
            listed, list_error = services.list_user_calendars(uid, access)
            if list_error:
                access, google_error = None, list_error
            else:
                with connect() as conn:
                    repo.sync_calendar_list(conn, uid, listed)

    with connect() as conn:
        tasks = repo.fetch_tasks(conn, uid)
        blocks = repo.fetch_blocks(conn, uid)
        chunks = repo.fetch_chunks(conn, uid)
        calendars = repo.fetch_calendars(conn, uid)
        dismissals = repo.fetch_dismissals(conn, uid)

    google_events: list[dict] = []
    if access:
        selected = [c["id"] for c in calendars if c["selected"]]
        google_events, fetch_error = services.fetch_user_events(
            uid, access, selected, now, now + GOOGLE_LOAD_WINDOW, tz
        )
        google_error = google_error or fetch_error

    return {
        "user": {"email": user["email"], "timezone": user["timezone"]},
        "settings": repo.settings_dict(user),
        "calendars": calendars,
        "tasks": tasks,
        "blocks": blocks,
        "chunks": chunks,
        "dismissed_events": dismissals,
        "google_events": google_events,
        "server_time": now,
        "google_error": google_error,
    }


@router.patch("/sync", response_model=SyncResponse, summary="Save a batch of changes (idempotent)")
def sync(body: SyncRequest, user: dict = Depends(auth.require_user)):
    with connect() as conn:
        skipped = repo.apply_sync(conn, user["id"], body)
    return {"ok": True, "server_time": datetime.now(timezone.utc), "skipped": skipped}


@router.delete("/account", status_code=204, summary="Delete the account and all its data")
def delete_account(user: dict = Depends(auth.require_user)) -> Response:
    uid = user["id"]
    refresh = None
    try:
        with connect() as conn:
            refresh = repo.get_refresh_token(conn, uid)
    except crypto.CryptoNotConfigured:
        pass
    with connect() as conn:
        repo.delete_user(conn, uid)
    google.forget_access_token(str(uid))
    if refresh:
        google.revoke(refresh)  # best effort; the local data is already gone
    return Response(status_code=204)
