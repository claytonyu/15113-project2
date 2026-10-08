"""Dismissing Google events inside Penciled In (SPEC: "Dismissing events")."""
from __future__ import annotations

import uuid

import pytest

from fake_google import timed_event
from frontend import local_dt, make_task


def dismissal(calendar_id: str, google_event_id: str, scope: str = "occurrence", id: str | None = None) -> dict:
    return {"id": id or str(uuid.uuid4()), "calendar_id": calendar_id, "google_event_id": google_event_id, "scope": scope}


@pytest.fixture
def student(google, signed_in):
    fe = signed_in()
    sub = fe.sub
    google.add_event(sub, "classes@test", timed_event("cs_mon", "CS class", local_dt(1, 13), local_dt(1, 14), recurringEventId="cs"))
    google.add_event(sub, "classes@test", timed_event("cs_tue", "CS class", local_dt(2, 13), local_dt(2, 14), recurringEventId="cs"))
    google.add_event(sub, "classes@test", timed_event("one_off", "Review session", local_dt(3, 13), local_dt(3, 14)))
    fe.sync({"calendars": [{"id": "classes@test", "selected": True}]})
    return fe


def flags(fe) -> dict:
    return {e["id"]: e["dismissed"] for e in fe.state().json()["google_events"]}


def test_nothing_is_dismissed_by_default(student):
    assert flags(student) == {"cs_mon": False, "cs_tue": False, "one_off": False}


def test_dismissing_one_occurrence_flags_only_that_event_but_still_returns_it(student):
    assert student.sync({"dismissed_events": {"upsert": [dismissal("classes@test", "cs_mon")]}}).status_code == 200
    assert flags(student) == {"cs_mon": True, "cs_tue": False, "one_off": False}


def test_dismissing_a_series_flags_every_instance(student):
    student.sync({"dismissed_events": {"upsert": [dismissal("classes@test", "cs", "series")]}})  # the recurring event ID
    assert flags(student) == {"cs_mon": True, "cs_tue": True, "one_off": False}


def test_a_dismissal_applies_on_every_device(google, student, make_frontend):
    student.sync({"dismissed_events": {"upsert": [dismissal("classes@test", "one_off")]}})
    phone = make_frontend()
    assert phone.login(google, student.sub).error is None
    assert flags(phone)["one_off"] is True
    assert len(phone.state().json()["dismissed_events"]) == 1


def test_restoring_is_deleting_the_dismissal(student):
    d = dismissal("classes@test", "cs_mon")
    student.sync({"dismissed_events": {"upsert": [d]}})
    assert flags(student)["cs_mon"] is True
    student.sync({"dismissed_events": {"delete": [d["id"]]}})
    assert flags(student)["cs_mon"] is False
    assert student.sync({"dismissed_events": {"delete": [d["id"]]}}).json()["skipped"] == []  # deleting twice is harmless


def test_resending_the_same_dismissal_is_idempotent(student):
    d = dismissal("classes@test", "cs_mon")
    for _ in range(3):
        body = student.sync({"dismissed_events": {"upsert": [d]}}).json()
        assert body["ok"] and body["skipped"] == []
    assert len(student.state().json()["dismissed_events"]) == 1


def test_a_duplicate_dismissal_from_another_device_is_skipped_without_failing_the_batch(student):
    first = dismissal("classes@test", "cs_mon")
    student.sync({"dismissed_events": {"upsert": [first]}})

    task = make_task("Essay", local_dt(5, 17), 60)
    second = dismissal("classes@test", "cs_mon")  # same event + scope, different ID
    body = student.sync({"tasks": {"upsert": [task]}, "dismissed_events": {"upsert": [second]}}).json()

    assert body["ok"] is True
    assert body["skipped"] == [{"collection": "dismissed_events", "id": second["id"], "reason": "duplicate_dismissal"}]
    state = student.state().json()
    assert [d["id"] for d in state["dismissed_events"]] == [first["id"]]  # the existing one stays
    assert [t["id"] for t in state["tasks"]] == [task["id"]]  # and the rest of the batch was saved


def test_a_dismissal_for_a_calendar_that_does_not_exist_is_skipped(student):
    ghost = dismissal("gone@test", "whatever")
    body = student.sync({"dismissed_events": {"upsert": [ghost]}}).json()
    assert body["skipped"] == [{"collection": "dismissed_events", "id": ghost["id"], "reason": "unknown_calendar"}]


def test_the_same_event_can_be_dismissed_as_occurrence_and_as_series(student):
    student.sync({"dismissed_events": {"upsert": [dismissal("classes@test", "cs", "series"), dismissal("classes@test", "cs", "occurrence")]}})
    assert len(student.state().json()["dismissed_events"]) == 2


def test_a_dismissal_outlives_the_event_it_hid(google, student):
    student.sync({"dismissed_events": {"upsert": [dismissal("classes@test", "one_off")]}})
    google.remove_event(student.sub, "classes@test", "one_off")  # deleted in Google
    state = student.state().json()
    assert "one_off" not in {e["id"] for e in state["google_events"]}
    assert len(state["dismissed_events"]) == 1  # harmless row stays


def test_dismissing_never_touches_google(google, student):
    student.sync({"dismissed_events": {"upsert": [dismissal("classes@test", "cs_mon")]}})
    student.state()
    assert {r.method for r in google.calls(path_part="/calendar/")} == {"GET"}
    assert google.revoked == []
