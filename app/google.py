"""Google OAuth 2.0 and read-only Google Calendar access.

We only ever read. Access tokens are short-lived and kept in memory; refresh tokens are stored
encrypted (see crypto.py). Failures are reported as GoogleError codes so the rest of a response
can still be returned:

- "reauth_required": Google rejected the refresh token (revoked, or expired after 7 days while
                     the OAuth app is in Testing status). The user must log in again.
- "google_unavailable": network error, rate limit, or a Google outage.
- "not_configured" / "not_connected": Google env vars missing / user has no Google token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from urllib.parse import urlencode

import httpx

from . import crypto
from .config import get_config

UTC = timezone.utc
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = "openid email https://www.googleapis.com/auth/calendar.readonly"
STATE_COOKIE = "tp_oauth_state"
STATE_TTL_S = 600

_client = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))


class GoogleError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------- login state (CSRF + PKCE)

def _state_key() -> bytes:
    secret = get_config().token_encryption_key or ""
    return hashlib.sha256(b"taskplanner-oauth-state:" + secret.encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_login_request() -> tuple[str, str]:
    """Returns (google_auth_url, signed_cookie_value).

    The cookie binds the flow to this browser: the callback only succeeds if the `state` in
    the URL matches the signed value in the cookie. It also carries the PKCE verifier.
    """
    cfg = get_config()
    if not cfg.google_configured:
        raise GoogleError("not_configured", "Google login is not configured on this server")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    payload = _b64(json.dumps({"s": state, "v": verifier, "exp": int(time.time()) + STATE_TTL_S}).encode())
    signature = _b64(hmac.new(_state_key(), payload.encode(), hashlib.sha256).digest())
    params = {
        "client_id": cfg.google_client_id,
        "redirect_uri": cfg.google_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # ensures Google returns a refresh token
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}", f"{payload}.{signature}"


def verify_login_state(cookie_value: str | None, state: str | None) -> str:
    """Returns the PKCE verifier if the cookie is valid, unexpired, and matches `state`."""
    if not cookie_value or not state or "." not in cookie_value:
        raise GoogleError("invalid_state", "missing login state")
    payload, signature = cookie_value.rsplit(".", 1)
    expected = _b64(hmac.new(_state_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise GoogleError("invalid_state", "bad login state signature")
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleError("invalid_state", "unreadable login state") from exc
    if data.get("exp", 0) < time.time() or not hmac.compare_digest(str(data.get("s", "")), state):
        raise GoogleError("invalid_state", "login state expired or mismatched")
    return str(data["v"])


# ------------------------------------------------------------------------- token handling

def _token_request(data: dict) -> dict:
    cfg = get_config()
    payload = {"client_id": cfg.google_client_id, "client_secret": cfg.google_client_secret, **data}
    try:
        resp = _client.post(TOKEN_URL, data=payload)
    except httpx.HTTPError as exc:
        raise GoogleError("google_unavailable", str(exc)) from exc
    if resp.status_code == 200:
        return resp.json()
    try:
        error = resp.json().get("error", "")
    except ValueError:
        error = ""
    if error == "invalid_grant":
        raise GoogleError("reauth_required", "Google rejected the grant")
    raise GoogleError("google_unavailable", f"token endpoint returned {resp.status_code} {error}")


def exchange_code(code: str, verifier: str) -> dict:
    cfg = get_config()
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.google_redirect_uri,
            "code_verifier": verifier,
        }
    )


_access_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def access_token_for(user_id: str, refresh_token: str) -> str:
    """A valid access token for the user, refreshed when needed. Memory-only cache."""
    key = str(user_id)
    with _cache_lock:
        cached = _access_cache.get(key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
    data = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    token = data["access_token"]
    with _cache_lock:
        _access_cache[key] = (token, time.time() + int(data.get("expires_in", 3000)))
    return token


def forget_access_token(user_id: str) -> None:
    with _cache_lock:
        _access_cache.pop(str(user_id), None)


def revoke(token: str) -> None:
    """Best effort: failures are ignored because the local data is deleted regardless."""
    try:
        _client.post(REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        pass


# --------------------------------------------------------------------------- API helpers

def _get(url: str, access_token: str, params: dict | None = None) -> dict:
    try:
        resp = _client.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise GoogleError("google_unavailable", str(exc)) from exc
    if resp.status_code == 401:
        raise GoogleError("reauth_required", "access token rejected")
    if resp.status_code != 200:
        raise GoogleError("google_unavailable", f"Google returned {resp.status_code}")
    return resp.json()


def get_userinfo(access_token: str) -> dict:
    return _get(USERINFO_URL, access_token)


def list_calendars(access_token: str) -> list[dict]:
    """[{id, name, primary, time_zone}] for every calendar the user can see."""
    out: list[dict] = []
    page_token = None
    while True:
        params = {"maxResults": 250, "fields": "nextPageToken,items(id,summary,summaryOverride,primary,timeZone)"}
        if page_token:
            params["pageToken"] = page_token
        data = _get(f"{CALENDAR_API}/users/me/calendarList", access_token, params)
        for item in data.get("items", []):
            out.append(
                {
                    "id": item["id"],
                    "name": item.get("summaryOverride") or item.get("summary") or item["id"],
                    "primary": bool(item.get("primary")),
                    "time_zone": item.get("timeZone"),
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            return out


def _parse_when(value: dict, tz: tzinfo) -> datetime | None:
    if "dateTime" in value:
        parsed = datetime.fromisoformat(value["dateTime"])
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(UTC)
    if "date" in value:  # all-day events: local midnight in the user's timezone
        d = date.fromisoformat(value["date"])
        return datetime.combine(d, dtime.min, tzinfo=tz).astimezone(UTC)
    return None


def list_events(access_token: str, calendar_id: str, time_min: datetime, time_max: datetime, tz: tzinfo) -> list[dict]:
    """Events as dicts {id, recurring_event_id, calendar_id, title, start, end}.

    Recurring events arrive already expanded (singleEvents=true). Cancelled events, events
    marked 'free', and events the user declined are skipped. All-day events are included.
    """
    from urllib.parse import quote

    out: list[dict] = []
    page_token = None
    url = f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events"
    while True:
        params = {
            "timeMin": time_min.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeMax": time_max.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 2500,
            "fields": "nextPageToken,items(id,status,summary,transparency,recurringEventId,start,end,attendees(self,responseStatus))",
        }
        if page_token:
            params["pageToken"] = page_token
        data = _get(url, access_token, params)
        for item in data.get("items", []):
            if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
                continue
            if any(a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", [])):
                continue
            start = _parse_when(item.get("start", {}), tz)
            end = _parse_when(item.get("end", {}), tz)
            if start is None or end is None or end <= start:
                continue
            out.append(
                {
                    "id": item["id"],
                    "recurring_event_id": item.get("recurringEventId"),
                    "calendar_id": calendar_id,
                    "title": item.get("summary", ""),
                    "start": start,
                    "end": end,
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            return out


def fetch_events(
    access_token: str, calendar_ids: list[str], time_min: datetime, time_max: datetime, tz: tzinfo
) -> tuple[list[dict], str | None]:
    """Fetch several calendars in parallel. Returns (events, error_code_or_None).

    If one calendar fails, events from the others are still returned along with the error.
    """
    if not calendar_ids or time_max <= time_min:
        return [], None
    events: list[dict] = []
    error: str | None = None
    with ThreadPoolExecutor(max_workers=min(5, len(calendar_ids))) as pool:
        futures = [pool.submit(list_events, access_token, cid, time_min, time_max, tz) for cid in calendar_ids]
        for future in futures:
            try:
                events.extend(future.result())
            except GoogleError as exc:
                if error is None or exc.code == "reauth_required":
                    error = exc.code
    return events, error


def mark_dismissed(events: list[dict], dismissals: list[dict]) -> list[dict]:
    """Adds a `dismissed` flag to each event based on stored dismissals."""
    occurrences = {(d["calendar_id"], d["google_event_id"]) for d in dismissals if d["scope"] == "occurrence"}
    series = {(d["calendar_id"], d["google_event_id"]) for d in dismissals if d["scope"] == "series"}
    for ev in events:
        ev["dismissed"] = (
            (ev["calendar_id"], ev["id"]) in occurrences
            or (ev.get("recurring_event_id") is not None and (ev["calendar_id"], ev["recurring_event_id"]) in series)
        )
    return events
