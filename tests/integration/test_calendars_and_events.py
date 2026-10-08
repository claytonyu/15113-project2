"""Calendars and Google events as the frontend sees them through GET /state and PATCH /sync."""
from __future__ import annotations

import pytest

from fake_google import all_day_event, timed_event
from frontend import NY, UTC, local_dt, local_day, parse, zulu
from datetime import datetime, timedelta


@pytest.fixture
def busy_week(google, signed_in):
    """A signed-in student whose Google calendars hold a mix of events."""
    fe = signed_in(calendars=[
        {"id": "primary@test", "name": "Me", "primary": True},
        {"id": "classes@test", "name": "Classes"},
        {"id": "holidays@test", "name": "Holidays"},
    ])
    sub = fe.sub
    google.add_event(sub, "primary@test", timed_event("lunch", "Lunch meeting", local_dt(1, 10), local_dt(1, 11, 30)))
    google.add_event(sub, "primary@test", all_day_event("trip", "Family trip", local_day(2)))
    google.add_event(sub, "primary@test", timed_event("free", "Marked free", local_dt(1, 14), local_dt(1, 15), transparency="transparent"))
    google.add_event(sub, "primary@test", timed_event(
        "declined", "Declined invite", local_dt(1, 16), local_dt(1, 17),
        attendees=[{"self": True, "responseStatus": "declined"}, {"email": "x@y.z", "responseStatus": "accepted"}]))
    google.add_event(sub, "primary@test", timed_event("cancelled", "Cancelled", local_dt(1, 18), local_dt(1, 19), status="cancelled"))
    google.add_event(sub, "primary@test", timed_event("far", "Far future", local_dt(70, 10), local_dt(70, 11)))
    google.add_event(sub, "classes@test", timed_event("cs_mon", "CS class", local_dt(1, 13), local_dt(1, 14), recurringEventId="cs"))
    google.add_event(sub, "classes@test", timed_event("cs_tue", "CS class", local_dt(2, 13), local_dt(2, 14), recurringEventId="cs"))
    google.add_event(sub, "holidays@test", timed_event("holiday", "Holiday", local_dt(1, 9), local_dt(1, 10)))
    fe.sync({"calendars": [{"id": "primary@test", "selected": True}, {"id": "classes@test", "selected": True}]})
    return fe


def events_by_id(state: dict) -> dict:
    return {e["id"]: e for e in state["google_events"]}


def test_state_returns_events_of_selected_calendars_only(busy_week):
    state = busy_week.state().json()
    events = events_by_id(state)

    assert set(events) == {"lunch", "trip", "cs_mon", "cs_tue"}  # not holidays (unselected), not "far" (>60 days)
    assert state["google_error"] is None


def test_ignored_events_never_reach_the_frontend(busy_week):
    events = events_by_id(busy_week.state().json())
    assert not {"free", "declined", "cancelled"} & set(events)  # free, declined, and cancelled events do not block


def test_event_times_are_exact_utc_and_all_day_events_start_at_local_midnight(busy_week):
    events = events_by_id(busy_week.state().json())

    lunch = events["lunch"]
    assert (parse(lunch["start"]), parse(lunch["end"])) == (local_dt(1, 10).astimezone(UTC), local_dt(1, 11, 30).astimezone(UTC))
    assert lunch["start"].endswith("Z") and lunch["title"] == "Lunch meeting" and lunch["calendar_id"] == "primary@test"
    assert lunch["recurring_event_id"] is None and lunch["dismissed"] is False

    trip = events["trip"]  # all-day events count as blocks, from midnight to midnight in the user's zone
    assert parse(trip["start"]) == local_dt(2, 0).astimezone(UTC)
    assert parse(trip["end"]) == local_dt(3, 0).astimezone(UTC)


def test_recurring_instances_arrive_expanded_with_their_series_id(busy_week):
    events = events_by_id(busy_week.state().json())
    assert events["cs_mon"]["recurring_event_id"] == events["cs_tue"]["recurring_event_id"] == "cs"
    assert events["cs_mon"]["id"] != events["cs_tue"]["id"]


def requested_window(google) -> tuple[datetime, datetime]:
    request = google.calls(path_part="/events")[-1]
    assert request.params["singleEvents"] == "true"
    return parse(request.params["timeMin"]), parse(request.params["timeMax"])


def test_events_are_requested_for_30_days_either_side_of_today_with_single_events(google, busy_week):
    busy_week.state()
    t_min, t_max = requested_window(google)
    assert t_min == local_dt(-30, 0).astimezone(UTC)  # local midnight, 30 days ago
    assert t_max == local_dt(30, 0).astimezone(UTC)   # local midnight, 30 days ahead


def test_offset_days_moves_the_window_and_defaults_to_zero(google, busy_week):
    busy_week.state(offset_days=-30)
    assert requested_window(google) == (local_dt(-60, 0).astimezone(UTC), local_dt(0, 0).astimezone(UTC))

    busy_week.state(offset_days=45)
    assert requested_window(google) == (local_dt(15, 0).astimezone(UTC), local_dt(75, 0).astimezone(UTC))

    busy_week.state(offset_days=0)
    assert requested_window(google) == (local_dt(-30, 0).astimezone(UTC), local_dt(30, 0).astimezone(UTC))


def test_offset_days_selects_which_events_come_back(google, signed_in):
    fe = signed_in()
    for name, day in (("past", -20), ("old", -45), ("soon", 5), ("later", 50)):
        google.add_event(fe.sub, "primary@test", timed_event(name, name, local_dt(day, 10), local_dt(day, 11)))
    fe.sync({"calendars": [{"id": "primary@test", "selected": True}]})

    assert set(events_by_id(fe.state().json())) == {"past", "soon"}  # past events are included now
    assert set(events_by_id(fe.state(offset_days=-30).json())) == {"past", "old"}
    assert set(events_by_id(fe.state(offset_days=45).json())) == {"later"}


def test_offset_days_out_of_range_is_rejected(busy_week):
    assert busy_week.state(offset_days=3651).status_code == 422
    assert busy_week.state(offset_days=-3651).status_code == 422
    assert busy_week.state(offset_days=3650).status_code == 200


def test_only_selected_calendars_are_fetched(google, busy_week):
    busy_week.state()
    fetched = {r.path for r in google.calls(path_part="/events")}
    assert not any("holidays" in p for p in fetched)


def test_state_without_google_sync_makes_no_google_calls_and_returns_no_events(google, busy_week):
    before = len(google.requests)
    state = busy_week.state(sync_google=False).json()
    assert len(google.requests) == before
    assert state["google_events"] == [] and state["google_error"] is None
    assert len(state["calendars"]) == 3  # the stored calendar list is still returned


def test_pagination_is_followed_for_calendars_and_events(google, busy_week):
    google.page_size = 1
    state = busy_week.state().json()
    assert set(events_by_id(state)) == {"lunch", "trip", "cs_mon", "cs_tue"}
    assert len(state["calendars"]) == 3
    assert len(google.calls(path_part="/events", method="GET")) >= 4  # several pages


def test_calendar_selection_and_padding_overrides_persist(busy_week):
    cal = lambda: {c["id"]: c for c in busy_week.state(sync_google=False).json()["calendars"]}
    assert cal()["classes@test"]["selected"] is True

    assert busy_week.sync({"calendars": [{"id": "classes@test", "padding_override_min": 5}]}).status_code == 200
    assert cal()["classes@test"]["padding_override_min"] == 5 and cal()["classes@test"]["selected"] is True  # key omitted: untouched

    busy_week.sync({"calendars": [{"id": "classes@test", "selected": False}]})
    assert cal()["classes@test"]["selected"] is False and cal()["classes@test"]["padding_override_min"] == 5

    busy_week.sync({"calendars": [{"id": "classes@test", "padding_override_min": None}]})  # null clears the override
    assert cal()["classes@test"]["padding_override_min"] is None

    assert busy_week.sync({"calendars": [{"id": "classes@test", "padding_override_min": 999}]}).status_code == 422


def test_calendars_added_in_google_appear_unselected_and_existing_choices_survive(google, busy_week):
    google.add_calendar(busy_week.sub, "new@test", "New calendar")
    calendars = {c["id"]: c for c in busy_week.state().json()["calendars"]}
    assert calendars["new@test"]["selected"] is False
    assert calendars["primary@test"]["selected"] is True


def test_calendars_removed_in_google_disappear_with_their_dismissals(google, busy_week, database):
    import uuid

    dismissal = {"id": str(uuid.uuid4()), "calendar_id": "classes@test", "google_event_id": "cs", "scope": "series"}
    assert busy_week.sync({"dismissed_events": {"upsert": [dismissal]}}).json()["skipped"] == []
    uid = database.user_id(busy_week.sub)
    assert database.count("dismissed_events", uid) == 1

    google.remove_calendar(busy_week.sub, "classes@test")
    state = busy_week.state().json()

    assert "classes@test" not in {c["id"] for c in state["calendars"]}
    assert state["dismissed_events"] == [] and database.count("dismissed_events", uid) == 0
    assert not any(e["calendar_id"] == "classes@test" for e in state["google_events"])

    # A stale client still holding the calendar gets a "skipped" entry, not an error that blocks its sync.
    body = busy_week.sync({"calendars": [{"id": "classes@test", "selected": True}]}).json()
    assert body["ok"] is True
    assert body["skipped"] == [{"collection": "calendars", "id": "classes@test", "reason": "unknown_calendar"}]


def test_the_backend_never_writes_to_the_users_google_calendar(google, busy_week):
    busy_week.state()
    busy_week.state(sync_google=False)
    assert {r.method for r in google.calls(path_part="/calendar/")} == {"GET"}
