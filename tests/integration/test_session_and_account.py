"""Logout, session expiry, account deletion, and multi-user isolation."""
from __future__ import annotations

import uuid

import pytest

from fake_google import timed_event
from frontend import SETTINGS, local_dt, make_block, make_task, parse


def populate(fe) -> dict:
    """Give the account one of everything the backend stores."""
    task = make_task("Essay", local_dt(3, 17), 120)
    block = make_block("Sleep", local_dt(3, 0), local_dt(3, 8))
    chunk = {"id": str(uuid.uuid4()), "task_id": task["id"], "start": local_dt(3, 9).isoformat(),
             "end": local_dt(3, 11).isoformat(), "locked": True}
    dismissal = {"id": str(uuid.uuid4()), "calendar_id": "classes@test", "google_event_id": "cs", "scope": "series"}
    response = fe.sync({
        "settings": {"padding_min": 20},
        "calendars": [{"id": "classes@test", "selected": True, "padding_override_min": 5}],
        "tasks": {"upsert": [task]}, "blocks": {"upsert": [block]}, "chunks": {"upsert": [chunk]},
        "dismissed_events": {"upsert": [dismissal]},
    })
    assert response.status_code == 200 and response.json()["skipped"] == []
    return {"task": task, "block": block, "chunk": chunk, "dismissal": dismissal}


def test_logout_ends_this_session_only(google, signed_in, make_frontend, database):
    laptop = signed_in()
    phone = make_frontend()
    assert phone.login(google, laptop.sub).error is None
    uid = database.user_id(laptop.sub)
    assert database.count("sessions", uid) == 2

    token = laptop.token
    assert laptop.logout().status_code == 204
    assert database.count("sessions", uid) == 1

    laptop.local_storage["token"] = token  # even if the old token is replayed
    assert laptop.state().status_code == 401
    assert phone.state().status_code == 200  # the other device is unaffected


def test_logging_out_twice_is_a_401_but_harmless(signed_in):
    fe = signed_in()
    token = fe.token
    assert fe.logout().status_code == 204
    fe.local_storage["token"] = token
    assert fe.logout().status_code == 401


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Basic abc123", "Bearer not-a-real-token", "token abc"])
def test_missing_or_bad_credentials_are_401_on_every_protected_endpoint(frontend, header):
    headers = {} if header is None else {"Authorization": header}
    for method, path, body in (("GET", "/state", None), ("PATCH", "/sync", {}), ("DELETE", "/auth/session", None), ("DELETE", "/account", None)):
        response = frontend.api(method, path, json=body, auth=False, headers=headers)
        assert response.status_code == 401, (method, path, header)
        assert "bearer" in response.headers["www-authenticate"].lower()
        assert isinstance(response.json()["detail"], str)


def test_an_expired_session_is_a_401_and_the_frontend_drops_its_token(signed_in, database):
    from conftest import sha256_hex

    fe = signed_in()
    assert fe.state().status_code == 200
    database.one("UPDATE sessions SET expires_at = now() - interval '1 minute' WHERE token_hash = %s RETURNING 1",
                 (sha256_hex(fe.token),))
    assert fe.state().status_code == 401
    assert fe.token is None


def test_deleting_the_account_removes_everything_and_revokes_the_google_grant(google, signed_in, database):
    fe = signed_in()
    populate(fe)
    uid = database.user_id(fe.sub)
    refresh = google.refresh_token_for(fe.sub)
    tables = ["sessions", "google_tokens", "calendars", "tasks", "blocks", "scheduled_chunks", "dismissed_events"]
    assert all(database.count(t, uid) >= 1 for t in tables)

    response = fe.delete_account()

    assert response.status_code == 204 and response.content == b""
    assert database.user_id(fe.sub) is None
    assert all(database.count(t, uid) == 0 for t in tables)
    assert google.revoked == [refresh]  # Google was told to forget the grant
    assert {r.method for r in google.calls(path_part="/calendar/")} == {"GET"}  # and nothing was written to the calendar
    assert fe.state().status_code == 401  # the session died with the account


def test_logging_in_after_deleting_the_account_starts_from_scratch(google, signed_in, make_frontend, database):
    fe = signed_in()
    populate(fe)
    old_id = database.user_id(fe.sub)
    fe.delete_account()

    again = make_frontend()
    assert again.login(google, fe.sub).error is None
    body = again.state().json()
    assert database.user_id(fe.sub) != old_id
    assert body["tasks"] == body["blocks"] == body["chunks"] == body["dismissed_events"] == []
    assert body["settings"]["padding_min"] == 0  # defaults again
    assert {c["selected"] for c in body["calendars"]} == {False}


def test_users_cannot_see_or_change_each_others_data(google, signed_in):
    alice, bob = signed_in(), signed_in()
    shared_id = str(uuid.uuid4())  # both clients happen to generate the same UUID
    alice_task = {**make_task("Alice's essay", local_dt(3, 17), 60), "id": shared_id}
    bob_task = {**make_task("Bob's lab", local_dt(3, 17), 90), "id": shared_id}
    assert alice.sync({"tasks": {"upsert": [alice_task]}}).status_code == 200
    assert bob.sync({"tasks": {"upsert": [bob_task]}}).status_code == 200

    assert [t["title"] for t in alice.state(sync_google=False).json()["tasks"]] == ["Alice's essay"]
    assert [t["title"] for t in bob.state(sync_google=False).json()["tasks"]] == ["Bob's lab"]

    bob.sync({"tasks": {"delete": [shared_id]}})  # deleting "the same ID" only affects Bob's own row
    assert [t["title"] for t in alice.state(sync_google=False).json()["tasks"]] == ["Alice's essay"]


def test_each_user_gets_only_their_own_google_events(google, signed_in):
    alice, bob = signed_in(), signed_in()
    google.add_event(alice.sub, "primary@test", timed_event("a1", "Alice only", local_dt(1, 10), local_dt(1, 11)))
    google.add_event(bob.sub, "primary@test", timed_event("b1", "Bob only", local_dt(1, 10), local_dt(1, 11)))
    for fe in (alice, bob):
        fe.sync({"calendars": [{"id": "primary@test", "selected": True}]})

    assert [e["id"] for e in alice.state().json()["google_events"]] == ["a1"]
    assert [e["id"] for e in bob.state().json()["google_events"]] == ["b1"]
    assert google.refresh_token_for(alice.sub) != google.refresh_token_for(bob.sub)


def test_a_user_cannot_attach_a_chunk_to_someone_elses_task(signed_in):
    alice, bob = signed_in(), signed_in()
    task = make_task("Alice's essay", local_dt(3, 17), 60)
    alice.sync({"tasks": {"upsert": [task]}})
    stolen = {"id": str(uuid.uuid4()), "task_id": task["id"], "start": local_dt(3, 9).isoformat(),
              "end": local_dt(3, 10).isoformat(), "locked": False}

    response = bob.sync({"chunks": {"upsert": [stolen]}})

    assert response.status_code == 422 and response.json()["ids"] == [stolen["id"]]
    assert alice.state(sync_google=False).json()["chunks"] == []
