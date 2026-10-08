"""Pressing Generate while logged in: Google events act as blocks (SPEC: "Schedule generation")."""
from __future__ import annotations

import uuid

import pytest

from fake_google import all_day_event, timed_event
from frontend import SETTINGS, local_dt, local_span, make_block, make_task, parse, zulu

# Scenario: one working day (9 to 5). A Google event sits from 10:00 to 12:00 with 30 minutes of
# padding, so the free time is 9:00-9:30 and 12:30-17:00: exactly 300 minutes.
DAY = 2


@pytest.fixture
def scenario(google, signed_in):
    fe = signed_in()
    google.add_event(fe.sub, "primary@test", timed_event("meeting", "Team meeting", local_dt(DAY, 10), local_dt(DAY, 12)))
    fe.sync({"calendars": [{"id": "primary@test", "selected": True}]})
    fe.task = make_task("Problem set", local_dt(DAY, 17), 300, splittable=True, min_chunk=15)
    return fe


def request(fe, **overrides) -> dict:
    body = {"tasks": [fe.task], "blocks": [], "settings": SETTINGS, "locked_chunks": [], "now": local_dt(DAY, 6).isoformat()}
    body.update(overrides)
    return body


def spans(response_json) -> list[tuple[str, str]]:
    return [local_span(c) for c in response_json["chunks"]]


def test_google_events_block_time_and_get_padded(scenario):
    response = scenario.schedule(request(scenario))
    body = response.json()

    assert response.status_code == 200
    assert spans(body) == [("09:00", "09:30"), ("12:30", "17:00")]  # around 10:00-12:00 plus 30 min padding each side
    assert body["unschedulable"] == [] and body["warnings"] == [] and body["google_error"] is None
    assert [e["id"] for e in body["google_events"]] == ["meeting"]
    assert all(c["locked"] is False for c in body["chunks"])


def test_google_events_block_time_the_same_way_manual_blocks_do(make_frontend, scenario):
    guest = make_frontend()  # not logged in: the same meeting entered by hand
    manual = make_block("Team meeting", local_dt(DAY, 10), local_dt(DAY, 12))
    guest_body = guest.schedule(request(scenario, blocks=[manual])).json()
    google_body = scenario.schedule(request(scenario)).json()
    assert spans(guest_body) == spans(google_body) == [("09:00", "09:30"), ("12:30", "17:00")]


def test_an_unselected_calendar_does_not_block_anything(scenario):
    scenario.sync({"calendars": [{"id": "primary@test", "selected": False}]})
    body = scenario.schedule(request(scenario)).json()
    assert spans(body) == [("09:00", "14:00")]
    assert body["google_events"] == []


def test_unsaved_calendar_choices_in_the_request_are_respected(scenario):
    scenario.sync({"calendars": [{"id": "primary@test", "selected": False}]})
    # The student ticks the box and sets a 0-minute padding, but nothing has been saved yet.
    chosen = [{"id": "primary@test", "selected": True, "padding_override_min": 0}]
    body = scenario.schedule(request(scenario, calendars=chosen)).json()

    assert spans(body)[0] == ("09:00", "10:00") and spans(body)[1][0] == "12:00"  # no padding around the event
    stored = {c["id"]: c for c in scenario.state(sync_google=False).json()["calendars"]}
    assert stored["primary@test"]["selected"] is False and stored["primary@test"]["padding_override_min"] is None


def test_a_saved_padding_override_replaces_the_global_padding(scenario):
    scenario.sync({"calendars": [{"id": "primary@test", "padding_override_min": 10}]})
    # 10 minutes of padding around 10:00-12:00 leaves 9:00-9:50 and 12:10 onwards (50 + 250 = 300 minutes).
    # Chunks start on the 15-minute grid, so the 12:10 opening is used from 12:15 (250 minutes: 12:15-16:25).
    assert spans(scenario.schedule(request(scenario)).json()) == [("09:00", "09:50"), ("12:15", "16:25")]


def test_dismissed_events_are_ignored_and_restoring_blocks_time_again(scenario):
    d = {"id": str(uuid.uuid4()), "calendar_id": "primary@test", "google_event_id": "meeting", "scope": "occurrence"}
    scenario.sync({"dismissed_events": {"upsert": [d]}})
    body = scenario.schedule(request(scenario)).json()
    assert spans(body) == [("09:00", "14:00")]  # the meeting no longer blocks anything
    assert body["google_events"][0]["dismissed"] is True  # but is still shown so it can be restored

    scenario.sync({"dismissed_events": {"delete": [d["id"]]}})
    assert spans(scenario.schedule(request(scenario)).json()) == [("09:00", "09:30"), ("12:30", "17:00")]


def test_events_marked_free_or_declined_do_not_block(google, scenario):
    google.add_event(scenario.sub, "primary@test", timed_event("free", "Free", local_dt(DAY, 13), local_dt(DAY, 14), transparency="transparent"))
    google.add_event(scenario.sub, "primary@test", timed_event(
        "no", "Declined", local_dt(DAY, 15), local_dt(DAY, 16), attendees=[{"self": True, "responseStatus": "declined"}]))
    assert spans(scenario.schedule(request(scenario)).json()) == [("09:00", "09:30"), ("12:30", "17:00")]


def test_an_all_day_event_blocks_the_whole_day(google, scenario):
    google.add_event(scenario.sub, "primary@test", all_day_event("holiday", "Holiday", local_dt(DAY, 0).date()))
    body = scenario.schedule(request(scenario)).json()
    assert body["chunks"] == []
    assert body["unschedulable"] == [{"task_id": scenario.task["id"], "missing_min": 300}]


def test_events_are_fetched_up_to_the_latest_due_date_not_just_60_days(google, signed_in):
    fe = signed_in()
    google.add_event(fe.sub, "primary@test", timed_event("far", "Far away", local_dt(70, 10), local_dt(70, 12)))
    fe.sync({"calendars": [{"id": "primary@test", "selected": True}]})
    assert fe.state().json()["google_events"] == []  # page load only covers 60 days

    fe.task = make_task("Thesis", local_dt(70, 17), 300, splittable=True, min_chunk=15)
    body = fe.schedule(request(fe, now=local_dt(70, 6).isoformat())).json()

    assert [e["id"] for e in body["google_events"]] == ["far"]
    assert spans(body) == [("09:00", "09:30"), ("12:30", "17:00")]
    asked = google.calls(path_part="/events")[-1].params
    assert asked["timeMax"] == zulu(local_dt(70, 17))  # up to the latest due date
    assert asked["timeMin"] == zulu(local_dt(70, 6))


def test_generating_never_writes_to_the_database_and_saving_is_a_separate_step(scenario, database):
    uid = database.user_id(scenario.sub)
    body = scenario.schedule(request(scenario)).json()
    assert database.count("tasks", uid) == database.count("scheduled_chunks", uid) == 0
    assert scenario.state(sync_google=False).json()["chunks"] == []

    # The frontend saves the task and the proposals with its next /sync.
    saved = scenario.sync({"tasks": {"upsert": [scenario.task]}, "chunks": {"upsert": body["chunks"]}})
    assert saved.status_code == 200
    stored = scenario.state(sync_google=False).json()["chunks"]
    assert sorted(c["id"] for c in stored) == sorted(c["id"] for c in body["chunks"])


def test_regenerating_keeps_locked_chunks_and_counts_them(scenario):
    first = scenario.schedule(request(scenario)).json()["chunks"]
    locked = {**first[0], "locked": True}  # the student clicks the 9:00 chunk to lock it
    scenario.sync({"tasks": {"upsert": [scenario.task]}, "chunks": {"upsert": [locked, first[1]]}})

    body = scenario.schedule(request(scenario, locked_chunks=[locked])).json()

    assert spans(body) == [("12:30", "17:00")]  # only the 270 missing minutes are proposed
    assert body["unschedulable"] == [] and body["warnings"] == []
    assert locked["id"] not in {c["id"] for c in body["chunks"]}  # the client already holds locked chunks


def test_a_locked_chunk_on_a_google_event_is_kept_with_a_warning(scenario):
    clash = {"id": str(uuid.uuid4()), "task_id": scenario.task["id"], "start": local_dt(DAY, 10, 30).isoformat(),
             "end": local_dt(DAY, 11, 30).isoformat(), "locked": True}
    body = scenario.schedule(request(scenario, locked_chunks=[clash])).json()
    assert [(w["code"], w["chunk_id"]) for w in body["warnings"]] == [("locked_chunk_overlaps_block", clash["id"])]


def test_a_logged_in_user_can_still_schedule_a_task_that_does_not_fit(scenario):
    big = make_task("Too big", local_dt(DAY, 17), 600, splittable=True, min_chunk=15)
    body = scenario.schedule(request(scenario, tasks=[big])).json()
    assert sum((parse(c["end"]) - parse(c["start"])).seconds // 60 for c in body["chunks"]) == 300
    assert body["unschedulable"] == [{"task_id": big["id"], "missing_min": 300}]


def test_guests_never_touch_google_or_the_database(google, make_frontend, database):
    guest = make_frontend()
    before_requests = len(google.requests)
    task = make_task("Essay", local_dt(DAY, 17), 120)
    response = guest.schedule({"tasks": [task], "blocks": [], "settings": SETTINGS, "now": local_dt(DAY, 6).isoformat()})
    assert response.status_code == 200
    assert spans(response.json()) == [("09:00", "11:00")]
    assert len(google.requests) == before_requests and response.json()["google_events"] == []


def test_a_stale_token_gets_a_401_and_the_frontend_falls_back_to_guest_mode(google, signed_in):
    fe = signed_in()
    fe.local_storage["token"] = "expired-or-bogus-token"
    task = make_task("Essay", local_dt(DAY, 17), 120)
    body = {"tasks": [task], "blocks": [], "settings": SETTINGS, "now": local_dt(DAY, 6).isoformat()}

    first = fe.schedule(body)
    assert first.status_code == 401 and fe.token is None  # the guide: clear the token on any 401
    assert fe.schedule(body).status_code == 200  # retried as a guest
