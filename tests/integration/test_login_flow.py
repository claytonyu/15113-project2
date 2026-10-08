"""Logging in with Google, as the frontend experiences it (FRONTEND_GUIDE.md section 3)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet

from conftest import FRONTEND_ORIGIN, SUB_PREFIX, sha256_hex
from fake_google import CLIENT_ID, READONLY_SCOPE


def new_sub() -> str:
    return SUB_PREFIX + uuid.uuid4().hex[:12]


def test_login_sends_the_browser_to_google_with_readonly_scope_state_and_pkce(google, frontend):
    response = frontend.start_login()
    url = urlsplit(response.headers["location"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}

    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == "http://localhost:8000/auth/google/callback"
    assert query["response_type"] == "code"
    assert set(query["scope"].split()) == {"openid", "email", READONLY_SCOPE}  # read-only, nothing wider
    assert query["access_type"] == "offline"  # we need a refresh token
    assert len(query["state"]) >= 20
    assert query["code_challenge_method"] == "S256" and query["code_challenge"]

    cookie = response.headers["set-cookie"].lower()
    assert "tp_oauth_state=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert response.headers["cache-control"] == "no-store"


def test_successful_login_hands_the_frontend_a_working_session(google, frontend, database):
    sub = new_sub()
    google.add_account(sub, "student@example.com", [
        {"id": "primary@test", "name": "Me", "primary": True, "tz": "America/Chicago"},
        {"id": "classes@test", "name": "Classes", "tz": "America/Chicago", "override": "My classes"},
    ])
    try:
        result = frontend.login(google, sub)

        assert result.error is None
        assert result.location.startswith(f"{FRONTEND_ORIGIN}/#token=")
        assert len(result.token) >= 32

        state = frontend.state()
        assert state.status_code == 200
        body = state.json()
        assert body["user"] == {"email": "student@example.com", "timezone": "America/Chicago"}
        assert body["settings"]["timezone"] == "America/Chicago"  # new users start in their primary calendar's zone
        assert body["google_error"] is None
        assert body["tasks"] == body["blocks"] == body["chunks"] == body["dismissed_events"] == []
        assert [(c["id"], c["name"], c["selected"], c["padding_override_min"]) for c in body["calendars"]] == [
            ("primary@test", "Me", False, None),  # new calendars start unselected, ordered by name
            ("classes@test", "My classes", False, None),  # summaryOverride (the user's own name) wins
        ]
        assert state.headers["cache-control"] == "no-store"
    finally:
        database.one("DELETE FROM users WHERE google_id = %s RETURNING id", (sub,))


def test_secrets_are_stored_safely(google, signed_in, database, integration_env):
    fe = signed_in()
    uid = database.user_id(fe.sub)

    stored = database.one("SELECT refresh_token_enc FROM google_tokens WHERE user_id = %s", (uid,))["refresh_token_enc"]
    refresh = google.refresh_token_for(fe.sub)
    assert stored != refresh and refresh not in stored  # encrypted at rest
    assert Fernet(integration_env.token_encryption_key.encode()).decrypt(stored.encode()).decode() == refresh

    # Only the hash of the session token is stored, and it expires in about 30 days.
    row = database.one("SELECT token_hash, expires_at FROM sessions WHERE user_id = %s", (uid,))
    assert row["token_hash"] == sha256_hex(fe.token) and row["token_hash"] != fe.token
    remaining = row["expires_at"] - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)

    # Access tokens are never stored: no table has a column for them.
    columns = {r["column_name"] for r in database.all(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema()")}
    assert not any("access" in c for c in columns)


def test_the_backend_exchanges_the_code_with_pkce_and_only_reads_the_calendar(google, signed_in):
    signed_in()
    exchange = google.token_calls("authorization_code")
    assert len(exchange) == 1
    assert exchange[0].params["code_verifier"] and exchange[0].params["redirect_uri"].endswith("/auth/google/callback")
    # Everything else it did at Google was a GET (FakeGoogle also records any write as a violation).
    assert {r.method for r in google.calls(path_part="/calendar/")} == {"GET"}


@pytest.mark.parametrize("problem", ["access_denied", "tampered_state", "no_code"])
def test_callback_problems_send_the_frontend_back_with_a_login_error(google, frontend, problem):
    auth_url = frontend.start_login().headers["location"]
    code, state = google.authorize(auth_url, new_sub())
    params = {
        "access_denied": {"error": "access_denied", "state": state},
        "tampered_state": {"code": code, "state": state + "x"},
        "no_code": {"state": state},
    }[problem]
    result = frontend.finish_login(params)

    assert result.token is None
    assert result.error == {"access_denied": "access_denied"}.get(problem, "invalid_state")
    assert frontend.token is None


def test_a_login_started_in_another_browser_cannot_be_completed_here(google, make_frontend):
    victim, attacker = make_frontend(), make_frontend()
    auth_url = victim.start_login().headers["location"]
    code, state = google.authorize(auth_url, new_sub())
    # The attacker has no state cookie, so a callback with the victim's code/state is rejected.
    assert attacker.finish_login({"code": code, "state": state}).error == "invalid_state"


def test_state_from_an_older_login_attempt_is_rejected(google, frontend):
    first = frontend.start_login().headers["location"]
    _, old_state = google.authorize(first, new_sub())
    second = frontend.start_login().headers["location"]  # replaces the cookie
    code, _ = google.authorize(second, new_sub())
    assert frontend.finish_login({"code": code, "state": old_state}).error == "invalid_state"


def test_google_rejecting_the_code_is_reported_as_reauth_required(google, frontend):
    auth_url = frontend.start_login().headers["location"]
    _, state = google.authorize(auth_url, new_sub())
    assert frontend.finish_login({"code": "not-a-real-code", "state": state}).error == "reauth_required"


def test_google_being_down_during_login_is_reported_and_creates_no_account(google, frontend, database):
    sub = new_sub()
    google.add_account(sub, "down@example.com", [{"id": "primary@test", "name": "Me", "primary": True}])
    google.fail_token_endpoint = 503
    result = frontend.login(google, sub)
    assert result.error == "google_unavailable" and result.token is None
    assert database.user_id(sub) is None


def test_logging_in_again_reuses_the_account_and_keeps_choices(google, signed_in, database, make_frontend):
    first = signed_in()
    first.sync({"settings": {"padding_min": 20}, "calendars": [{"id": "primary@test", "selected": True}]})

    second_device = make_frontend()
    assert second_device.login(google, first.sub).error is None

    body = second_device.state().json()
    assert body["settings"]["padding_min"] == 20
    assert {c["id"]: c["selected"] for c in body["calendars"]} == {"primary@test": True, "classes@test": False}
    assert database.one("SELECT count(*) AS n FROM users WHERE google_id = %s", (first.sub,))["n"] == 1
    assert first.state().status_code == 200  # the first session is still valid


def test_google_omitting_the_refresh_token_on_a_later_login_keeps_the_old_one(google, signed_in, make_frontend):
    first = signed_in()
    first.sync({"calendars": [{"id": "primary@test", "selected": True}]})
    google.issue_refresh_token = False  # Google only returns a refresh token on some consents

    again = make_frontend()
    assert again.login(google, first.sub).error is None
    assert again.state().json()["google_error"] is None


def test_the_frontend_origin_may_call_the_api_and_others_may_not(frontend):
    allowed = frontend.http.options("/state", headers={
        "Origin": FRONTEND_ORIGIN, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization"})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert "authorization" in allowed.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in allowed.headers  # bearer tokens, no cookies

    patch = frontend.http.options("/sync", headers={"Origin": FRONTEND_ORIGIN, "Access-Control-Request-Method": "PATCH"})
    assert "PATCH" in patch.headers["access-control-allow-methods"]

    evil = frontend.http.options("/state", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers
