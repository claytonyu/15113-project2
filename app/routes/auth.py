"""Endpoints 1-3: Google login, OAuth callback, logout."""
from __future__ import annotations

from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .. import auth, google, repo
from ..config import get_config
from ..db import connect
from ..limiter import limiter

router = APIRouter(tags=["auth"])

_COOKIE_PATH = "/auth/google"


def _frontend_redirect(fragment: str) -> RedirectResponse:
    response = RedirectResponse(f"{get_config().frontend_url}/#{fragment}", status_code=302)
    response.delete_cookie(google.STATE_COOKIE, path=_COOKIE_PATH)
    # The fragment holds a secret; keep it out of caches and referrers.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/auth/google/login", summary="Start Google login (redirects to Google)")
@limiter.limit(lambda: get_config().auth_rate_limit)
def google_login(request: Request):
    try:
        url, cookie = google.make_login_request()
    except google.GoogleError as exc:
        raise HTTPException(503, "Google login is not configured on this server") from exc
    cfg = get_config()
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        google.STATE_COOKIE,
        cookie,
        max_age=google.STATE_TTL_S,
        httponly=True,
        secure=cfg.secure_cookies,
        samesite="lax",  # sent on Google's top-level redirect back to us
        path=_COOKIE_PATH,
    )
    return response


@router.get("/auth/google/callback", summary="Google redirects here after consent")
@limiter.limit(lambda: get_config().auth_rate_limit)
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if not get_config().google_configured:
        return _frontend_redirect("login_error=not_configured")
    if error:
        return _frontend_redirect(f"login_error={quote(error[:50], safe='')}")
    try:
        verifier = google.verify_login_state(request.cookies.get(google.STATE_COOKIE), state)
        if not code:
            raise google.GoogleError("invalid_state", "missing code")
        tokens = google.exchange_code(code, verifier)
        access = tokens["access_token"]
        info = google.get_userinfo(access)
        calendars = google.list_calendars(access)
    except google.GoogleError as exc:
        code = "reauth_required" if exc.code == google.ACCESS_TOKEN_REJECTED else exc.code
        return _frontend_redirect(f"login_error={quote(code, safe='')}")

    # New users start in their primary calendar's timezone.
    tz_name = next((c["time_zone"] for c in calendars if c["primary"] and c["time_zone"]), "UTC")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz_name = "UTC"

    with connect() as conn:
        uid = repo.upsert_google_user(
            conn,
            google_id=info["sub"],
            email=info.get("email", ""),
            default_timezone=tz_name,
            refresh_token=tokens.get("refresh_token"),
            calendars=calendars,
        )
        token = auth.create_session(conn, uid)
    return _frontend_redirect(f"token={quote(token, safe='')}")


@router.delete("/auth/session", status_code=204, summary="Log out")
def logout(user: dict = Depends(auth.require_user)) -> Response:
    auth.delete_session(user["token"])
    return Response(status_code=204)
