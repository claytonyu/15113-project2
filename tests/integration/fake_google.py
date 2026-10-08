"""A stand-in for Google's OAuth and Calendar servers.

It plugs in underneath the backend's HTTP client (an `httpx.MockTransport`), so the backend's real
code runs unchanged: building the consent URL, exchanging the code, refreshing tokens, listing
calendars, paging through events, revoking tokens.

What it checks, like Google would:
- the OAuth client ID/secret, redirect URI, single-use authorization codes, and the PKCE verifier;
- bearer access tokens (unknown or expired tokens get 401);
- refresh tokens (revoked or unknown ones get `invalid_grant`);
- that the backend only ever READS the calendar (any non-GET is recorded as a violation) and that it
  asks for events with `singleEvents=true`.

What it cannot check: that real Google returns exactly these shapes. The JSON here follows Google's
documented formats, but only a live login can prove that.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

import httpx

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


@dataclass
class Recorded:
    method: str
    host: str
    path: str
    params: dict


@dataclass
class _Account:
    email: str
    calendars: list[dict] = field(default_factory=list)
    events: dict[str, list[dict]] = field(default_factory=dict)


# ----------------------------------------------------------------------- event builders

def timed_event(event_id: str, title: str, start: datetime, end: datetime, **extra) -> dict:
    return {
        "id": event_id,
        "status": "confirmed",
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": str(start.tzinfo)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(end.tzinfo)},
        **extra,
    }


def all_day_event(event_id: str, title: str, day: date, days: int = 1, **extra) -> dict:
    return {
        "id": event_id,
        "status": "confirmed",
        "summary": title,
        "start": {"date": day.isoformat()},
        "end": {"date": (day + timedelta(days=days)).isoformat()},
        **extra,
    }


class FakeGoogle:
    def __init__(self, redirect_uri: str):
        self.redirect_uri = redirect_uri
        self.lock = threading.Lock()
        self.accounts: dict[str, _Account] = {}
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> account sub
        self.access_tokens: dict[str, tuple[str, float]] = {}  # access token -> (sub, expires_at)
        self.revoked: list[str] = []
        self.requests: list[Recorded] = []
        self.violations: list[str] = []
        # knobs tests can turn
        self.issue_refresh_token = True
        self.token_expires_in = 3600
        self.page_size: int | None = None
        self.fail_token_endpoint: int | None = None
        self.fail_calendar_list: int | None = None
        self.fail_events: dict[str, int] = {}  # calendar id -> HTTP status
        self.network_down = False

    # ------------------------------------------------------------------ test-side setup

    def add_account(self, sub: str, email: str, calendars: list[dict] | None = None) -> None:
        self.accounts[sub] = _Account(email=email)
        for cal in calendars or []:
            self.add_calendar(sub, **cal)

    def add_calendar(self, sub: str, id: str, name: str, primary: bool = False, tz: str = "America/New_York",
                     override: str | None = None) -> None:
        item = {"id": id, "summary": name, "timeZone": tz}
        if primary:
            item["primary"] = True
        if override:
            item["summaryOverride"] = override
        self.accounts[sub].calendars.append(item)
        self.accounts[sub].events.setdefault(id, [])

    def remove_calendar(self, sub: str, id: str) -> None:
        acct = self.accounts[sub]
        acct.calendars = [c for c in acct.calendars if c["id"] != id]
        acct.events.pop(id, None)

    def add_event(self, sub: str, calendar_id: str, event: dict) -> None:
        self.accounts[sub].events[calendar_id].append(event)

    def remove_event(self, sub: str, calendar_id: str, event_id: str) -> None:
        events = self.accounts[sub].events[calendar_id]
        events[:] = [e for e in events if e["id"] != event_id]

    def revoke_all_refresh_tokens(self, sub: str) -> None:
        """What happens when a Testing-mode refresh token expires or the user revokes access."""
        for token in [t for t, s in self.refresh_tokens.items() if s == sub]:
            del self.refresh_tokens[token]

    def invalidate_access_tokens(self) -> None:
        self.access_tokens.clear()

    def refresh_token_for(self, sub: str) -> str:
        return next(t for t, s in self.refresh_tokens.items() if s == sub)

    def calls(self, host_part: str = "", path_part: str = "", method: str | None = None) -> list[Recorded]:
        return [
            r for r in self.requests
            if host_part in r.host and path_part in r.path and (method is None or r.method == method)
        ]

    def token_calls(self, grant_type: str) -> list[Recorded]:
        return [r for r in self.calls(path_part="/token") if r.params.get("grant_type") == grant_type]

    # ------------------------------------------------------- the user's browser at Google

    def authorize(self, auth_url: str, sub: str) -> tuple[str, str]:
        """The user signs in and consents. Returns (code, state) as Google would put in the redirect."""
        parts = urlsplit(auth_url)
        assert parts.netloc == "accounts.google.com", auth_url
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["client_id"] == CLIENT_ID
        assert q["redirect_uri"] == self.redirect_uri
        assert q["response_type"] == "code"
        assert q.get("code_challenge_method") == "S256" and q.get("code_challenge")
        code = "code-" + secrets.token_urlsafe(12)
        self.codes[code] = {"sub": sub, "challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"]}
        return code, q["state"]

    # --------------------------------------------------------------------- HTTP surface

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        params = {k: v for k, v in url.params.items()}
        form: dict[str, str] = {}
        if request.method == "POST" and request.content:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        with self.lock:
            self.requests.append(Recorded(request.method, url.host, url.path, {**params, **form}))
        if self.network_down:
            raise httpx.ConnectError("simulated network failure", request=request)
        with self.lock:
            return self._route(request, params, form)

    def _route(self, request: httpx.Request, params: dict, form: dict) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        if host == "oauth2.googleapis.com" and path == "/token" and method == "POST":
            return self._token(form)
        if host == "oauth2.googleapis.com" and path == "/revoke" and method == "POST":
            token = form.get("token", "")
            self.revoked.append(token)
            self.refresh_tokens.pop(token, None)
            return httpx.Response(200, json={})

        if method != "GET":
            self.violations.append(f"non-GET request to Google: {method} {host}{path}")
            return httpx.Response(405, json={"error": {"code": 405}})

        sub = self._bearer(request)
        if sub is None:
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        account = self.accounts[sub]

        if host == "openidconnect.googleapis.com" and path == "/v1/userinfo":
            return httpx.Response(200, json={"sub": sub, "email": account.email, "email_verified": True})
        if host == "www.googleapis.com" and path == "/calendar/v3/users/me/calendarList":
            if self.fail_calendar_list:
                return httpx.Response(self.fail_calendar_list, json={"error": {"code": self.fail_calendar_list}})
            return httpx.Response(200, json=self._page(account.calendars, params))
        if host == "www.googleapis.com" and path.startswith("/calendar/v3/calendars/") and path.endswith("/events"):
            calendar_id = unquote(path[len("/calendar/v3/calendars/"):-len("/events")])
            return self._events(account, calendar_id, params)
        self.violations.append(f"unexpected request: {method} {host}{path}")
        return httpx.Response(404, json={"error": {"code": 404}})

    # ---------------------------------------------------------------------- endpoints

    def _token(self, form: dict) -> httpx.Response:
        if self.fail_token_endpoint:
            return httpx.Response(self.fail_token_endpoint, json={"error": "backend_error"})
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            info = self.codes.pop(form.get("code", ""), None)  # codes are single-use
            if info is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if form.get("redirect_uri") != info["redirect_uri"] or challenge != info["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = self._issue_access(info["sub"])
            if self.issue_refresh_token:
                refresh = "rt-" + secrets.token_urlsafe(24)
                self.refresh_tokens[refresh] = info["sub"]
                body["refresh_token"] = refresh
            return httpx.Response(200, json=body)
        if grant == "refresh_token":
            sub = self.refresh_tokens.get(form.get("refresh_token", ""))
            if sub is None:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json=self._issue_access(sub))
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _issue_access(self, sub: str) -> dict:
        token = "at-" + secrets.token_urlsafe(24)
        self.access_tokens[token] = (sub, time.time() + self.token_expires_in)
        return {"access_token": token, "expires_in": self.token_expires_in, "token_type": "Bearer", "scope": READONLY_SCOPE}

    def _bearer(self, request: httpx.Request) -> str | None:
        header = request.headers.get("authorization", "")
        entry = self.access_tokens.get(header.removeprefix("Bearer ").strip())
        if entry is None or entry[1] < time.time():
            return None
        return entry[0]

    def _page(self, items: list[dict], params: dict) -> dict:
        size = self.page_size or int(params.get("maxResults", 250))
        start = int(params.get("pageToken") or 0)
        out: dict = {"items": items[start:start + size]}
        if start + size < len(items):
            out["nextPageToken"] = str(start + size)
        return out

    def _events(self, account: _Account, calendar_id: str, params: dict) -> httpx.Response:
        if params.get("singleEvents") != "true":
            self.violations.append("events requested without singleEvents=true")
        if params.get("orderBy", "startTime") != "startTime":
            self.violations.append("events requested with an unexpected orderBy")
        if calendar_id not in account.events:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        if calendar_id in self.fail_events:
            status = self.fail_events[calendar_id]
            return httpx.Response(status, json={"error": {"code": status}})

        calendar_tz = ZoneInfo(next(c["timeZone"] for c in account.calendars if c["id"] == calendar_id))
        t_min = datetime.fromisoformat(params["timeMin"].replace("Z", "+00:00"))
        t_max = datetime.fromisoformat(params["timeMax"].replace("Z", "+00:00"))

        def bounds(ev: dict) -> tuple[datetime, datetime]:
            def one(v: dict) -> datetime:
                if "dateTime" in v:
                    return datetime.fromisoformat(v["dateTime"]).astimezone(timezone.utc)
                d = date.fromisoformat(v["date"])
                return datetime(d.year, d.month, d.day, tzinfo=calendar_tz).astimezone(timezone.utc)
            return one(ev["start"]), one(ev["end"])

        # Google returns events overlapping [timeMin, timeMax), including declined/free/cancelled ones.
        inside = [e for e in account.events[calendar_id] if bounds(e)[0] < t_max and bounds(e)[1] > t_min]
        inside.sort(key=lambda e: bounds(e)[0])
        return httpx.Response(200, json=self._page(inside, params))
