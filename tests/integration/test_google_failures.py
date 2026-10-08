"""When Google misbehaves, the rest of the response must still come back (SPEC: Google Integration)."""
from __future__ import annotations

import pytest

from fake_google import timed_event
from frontend import SETTINGS, local_dt, local_span, make_block, make_task

DAY = 2


@pytest.fixture
def student(google, signed_in):
    """Logged in, one selected calendar with one event, and some saved data of their own."""
    fe = signed_in()
    google.add_event(fe.sub, "primary@test", timed_event("meeting", "Meeting", local_dt(1, 10), local_dt(1, 11)))
    google.add_event(fe.sub, "classes@test", timed_event("class", "Class", local_dt(1, 13), local_dt(1, 14)))
    fe.task = make_task("Essay", local_dt(DAY, 17), 120)
    fe.block = make_block("Sleep", local_dt(DAY, 0), local_dt(DAY, 8))
    saved = fe.sync({
        "calendars": [{"id": "primary@test", "selected": True}, {"id": "classes@test", "selected": True}],
        "tasks": {"upsert": [fe.task]}, "blocks": {"upsert": [fe.block]},
    })
    assert saved.status_code == 200
    return fe


def schedule_body(fe) -> dict:
    return {"tasks": [fe.task], "blocks": [fe.block], "settings": SETTINGS, "now": local_dt(DAY, 6).isoformat()}


def forget_cached_access_token():
    from app import google as backend_google

    backend_google._access_cache.clear()


def assert_own_data_intact(body: dict, fe) -> None:
    assert [t["id"] for t in body["tasks"]] == [fe.task["id"]]
    assert [b["id"] for b in body["blocks"]] == [fe.block["id"]]
    assert {c["id"] for c in body["calendars"]} == {"primary@test", "classes@test"}


def test_a_rejected_refresh_token_reports_reauth_required_but_returns_everything_else(google, student):
    google.revoke_all_refresh_tokens(student.sub)  # expired (Testing mode, ~7 days) or revoked by the user
    forget_cached_access_token()

    response = student.state()
    body = response.json()
    assert response.status_code == 200
    assert body["google_error"] == "reauth_required"
    assert body["google_events"] == []
    assert_own_data_intact(body, student)

    plan = student.schedule(schedule_body(student))
    assert plan.status_code == 200
    assert plan.json()["google_error"] == "reauth_required"
    assert [local_span(c) for c in plan.json()["chunks"]] == [("09:00", "11:00")]  # still planned, from manual blocks only

    assert student.state(sync_google=False).json()["google_error"] is None  # no Google call, no complaint


def test_logging_in_again_after_reauth_required_restores_google(google, student, make_frontend):
    google.revoke_all_refresh_tokens(student.sub)
    forget_cached_access_token()
    assert student.state().json()["google_error"] == "reauth_required"

    fresh = make_frontend()
    assert fresh.login(google, student.sub).error is None  # the "Reconnect Google" button
    body = fresh.state().json()
    assert body["google_error"] is None
    assert {e["id"] for e in body["google_events"]} == {"meeting", "class"}
    assert_own_data_intact(body, student)  # same account, nothing lost


def test_google_returning_500_for_the_calendar_list_is_reported_as_unavailable(google, student):
    google.fail_calendar_list = 500
    response = student.state()
    assert response.status_code == 200
    assert response.json()["google_error"] == "google_unavailable"
    assert_own_data_intact(response.json(), student)
    google.fail_calendar_list = None
    assert student.state().json()["google_error"] is None  # and it recovers by itself


def test_one_failing_calendar_does_not_hide_the_events_of_the_others(google, student):
    google.fail_events["classes@test"] = 500
    body = student.state().json()
    assert body["google_error"] == "google_unavailable"
    assert {e["id"] for e in body["google_events"]} == {"meeting"}

    plan = student.schedule(schedule_body(student)).json()
    assert plan["google_error"] == "google_unavailable" and plan["chunks"]


def test_the_token_endpoint_being_down_is_reported_as_unavailable(google, student):
    forget_cached_access_token()
    google.fail_token_endpoint = 503
    body = student.state().json()
    assert body["google_error"] == "google_unavailable"
    assert_own_data_intact(body, student)


def test_a_network_failure_is_reported_as_unavailable(google, student):
    forget_cached_access_token()
    google.network_down = True
    response = student.state()
    assert response.status_code == 200 and response.json()["google_error"] == "google_unavailable"
    google.network_down = False
    assert student.state().json()["google_error"] is None


def test_a_rotated_encryption_key_means_the_stored_grant_is_unreadable_so_reauth_is_required(student, monkeypatch):
    from app.config import get_config
    from cryptography.fernet import Fernet

    forget_cached_access_token()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_config.cache_clear()
    try:
        body = student.state().json()
        assert body["google_error"] == "reauth_required"
        assert_own_data_intact(body, student)
    finally:
        monkeypatch.undo()
        get_config.cache_clear()


def test_an_access_token_google_no_longer_accepts_is_refreshed_instead_of_forcing_a_new_login(google, student):
    assert student.state().json()["google_error"] is None  # first call caches an access token
    google.invalidate_access_tokens()  # Google stops accepting it early; the refresh token is still fine
    body = student.state().json()
    assert body["google_error"] is None
    assert {e["id"] for e in body["google_events"]} == {"meeting", "class"}


def test_generating_also_recovers_from_a_stale_access_token(google, student):
    assert student.state().json()["google_error"] is None
    google.invalidate_access_tokens()
    body = student.schedule(schedule_body(student)).json()
    assert body["google_error"] is None and body["chunks"]


def test_a_stale_access_token_and_a_revoked_refresh_token_together_need_a_new_login(google, student):
    assert student.state().json()["google_error"] is None
    google.invalidate_access_tokens()
    google.revoke_all_refresh_tokens(student.sub)
    response = student.state()
    assert response.status_code == 200 and response.json()["google_error"] == "reauth_required"
    assert_own_data_intact(response.json(), student)


def test_access_tokens_are_cached_between_requests_and_refreshed_when_about_to_expire(google, student):
    forget_cached_access_token()
    before = len(google.token_calls("refresh_token"))
    for _ in range(3):
        student.state()
    assert len(google.token_calls("refresh_token")) - before == 1  # one refresh served all three requests

    google.token_expires_in = 30  # shorter than the 60 second safety margin: never reused
    forget_cached_access_token()
    before = len(google.token_calls("refresh_token"))
    for _ in range(3):
        student.state()
    assert len(google.token_calls("refresh_token")) - before == 3
