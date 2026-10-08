"""A tiny stand-in for the real frontend, written from FRONTEND_GUIDE.md.

It talks to the backend only through HTTP, the way a browser would: it follows the login redirects by
hand, keeps the session token in a fake `localStorage` dict, sends `Authorization: Bearer`, and drops
its token on any 401.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from fake_google import FakeGoogle

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# Working hours 9 to 5 New York time, 30 minutes of padding, earliest slots first.
SETTINGS = {
    "padding_min": 30,
    "work_start": "09:00",
    "work_end": "17:00",
    "spread_mode": "front_load",
    "timezone": "America/New_York",
}


def local_dt(days: int, hour: int, minute: int = 0, tz: ZoneInfo = NY) -> datetime:
    """A wall-clock time `days` days from today in `tz`."""
    d = datetime.now(tz).date() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)


def local_day(days: int, tz: ZoneInfo = NY) -> date:
    return (datetime.now(tz) + timedelta(days=days)).date()


def zulu(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def make_task(title: str, due: datetime, minutes: int, splittable: bool = False, min_chunk: int | None = None) -> dict:
    task = {"id": str(uuid.uuid4()), "title": title, "due_at": due.isoformat(), "duration_min": minutes,
            "splittable": splittable}
    if min_chunk is not None:
        task["min_chunk_min"] = min_chunk
    return task


def make_block(title: str, start: datetime, end: datetime, rrule: str | None = None) -> dict:
    return {"id": str(uuid.uuid4()), "title": title, "start": start.isoformat(), "end": end.isoformat(), "rrule": rrule}


def local_span(chunk: dict, tz: ZoneInfo = NY) -> tuple[str, str]:
    """('09:00', '09:30') for a chunk, in local time. Easy to read in assertions."""
    return (parse(chunk["start"]).astimezone(tz).strftime("%H:%M"), parse(chunk["end"]).astimezone(tz).strftime("%H:%M"))


@dataclass
class LoginResult:
    token: str | None
    error: str | None
    location: str


class Frontend:
    def __init__(self, app, frontend_origin: str, backend_url: str = "http://localhost:8000"):
        self.http = TestClient(app, base_url=backend_url)
        self.origin = frontend_origin
        self.local_storage: dict[str, str] = {}

    @property
    def token(self) -> str | None:
        return self.local_storage.get("token")

    # ------------------------------------------------------------------------- plumbing

    def api(self, method: str, path: str, *, json=None, params=None, auth: bool = True, headers: dict | None = None):
        sent = {"Origin": self.origin, **(headers or {})}
        used_token = auth and self.token
        if used_token:
            sent["Authorization"] = f"Bearer {self.token}"
        response = self.http.request(method, path, json=json, params=params, headers=sent)
        if response.status_code == 401 and used_token:
            self.local_storage.pop("token", None)  # the guide: any 401 means the session is gone
        return response

    # ------------------------------------------------------------------------- login flow

    def start_login(self):
        """User clicks 'Log in with Google': the browser is redirected to Google."""
        response = self.http.get("/auth/google/login", follow_redirects=False)
        assert response.status_code == 302, response.text
        return response

    def finish_login(self, params: dict) -> LoginResult:
        """Google redirects the browser to the backend callback, which redirects to the frontend."""
        response = self.http.get("/auth/google/callback", params=params, follow_redirects=False)
        assert response.status_code == 302, response.text
        location = response.headers["location"]
        fragment = {k: v[0] for k, v in parse_qs(urlsplit(location).fragment).items()}
        token = fragment.get("token")
        if token:
            self.local_storage["token"] = token  # then the real frontend clears the fragment
        return LoginResult(token=token, error=fragment.get("login_error"), location=location)

    def login(self, google: FakeGoogle, sub: str) -> LoginResult:
        auth_url = self.start_login().headers["location"]
        code, state = google.authorize(auth_url, sub)
        return self.finish_login({"code": code, "state": state})

    # ------------------------------------------------------------------------- endpoints

    def state(self, sync_google: bool = True):
        return self.api("GET", "/state", params={"sync_google": str(sync_google).lower()})

    def sync(self, body: dict):
        return self.api("PATCH", "/sync", json=body)

    def schedule(self, body: dict):
        return self.api("POST", "/schedule", json=body)

    def logout(self):
        response = self.api("DELETE", "/auth/session")
        self.local_storage.pop("token", None)  # the client discards its token regardless of the result
        return response

    def delete_account(self):
        return self.api("DELETE", "/account")
