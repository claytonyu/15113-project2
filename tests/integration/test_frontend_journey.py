"""A whole visit, the way the guide describes it: guest, then login, then everyday use."""
from __future__ import annotations

import uuid

from fake_google import timed_event
from frontend import SETTINGS, local_dt, local_span, make_block, make_task


def test_guest_plans_a_week_then_logs_in_and_keeps_everything(google, make_frontend, signed_in, database):
    # 1. As a guest the only network call is POST /schedule, and no database or Google is involved.
    guest = make_frontend()
    task = make_task("Lab report", local_dt(2, 17), 120)
    block = make_block("Gym", local_dt(2, 9), local_dt(2, 10))
    body = {"tasks": [task], "blocks": [block], "settings": SETTINGS, "locked_chunks": [], "now": local_dt(2, 6).isoformat()}
    plan = guest.schedule(body).json()
    assert [local_span(c) for c in plan["chunks"]] == [("10:30", "12:30")]  # after the gym block and its padding
    assert google.requests == []

    # 2. The student logs in with Google; the frontend imports its local data with an ordinary PATCH /sync.
    fe = signed_in()
    fe.sync({"calendars": [{"id": "primary@test", "selected": True}]})
    imported = fe.sync({
        "settings": {"padding_min": SETTINGS["padding_min"], "work_start": "09:00", "work_end": "17:00", "spread_mode": "front_load"},
        "tasks": {"upsert": [task]}, "blocks": {"upsert": [block]}, "chunks": {"upsert": plan["chunks"]},
    })
    assert imported.status_code == 200 and imported.json()["skipped"] == []

    state = fe.state().json()
    assert [t["id"] for t in state["tasks"]] == [task["id"]]
    assert [b["id"] for b in state["blocks"]] == [block["id"]]
    assert [c["id"] for c in state["chunks"]] == [c["id"] for c in plan["chunks"]]
    assert state["settings"]["work_start"] == "09:00" and state["settings"]["padding_min"] == 30

    # 3. A Google meeting now appears; pressing Generate again routes around it.
    google.add_event(fe.sub, "primary@test", timed_event("m", "Meeting", local_dt(2, 10, 30), local_dt(2, 12)))
    again = fe.schedule({**body, "blocks": state["blocks"], "settings": {**SETTINGS, "timezone": state["settings"]["timezone"]}}).json()
    assert [e["id"] for e in again["google_events"]] == ["m"]
    assert [local_span(c) for c in again["chunks"]] == [("12:30", "14:30")]


def test_importing_guest_data_overwrites_matching_ids_and_leaves_the_rest(signed_in):
    fe = signed_in()
    old = make_task("Old title", local_dt(3, 17), 60)
    account_only = make_task("Only in my account", local_dt(4, 17), 30)
    fe.sync({"tasks": {"upsert": [old, account_only]}})

    guest_version = {**old, "title": "New title from my guest data", "duration_min": 90}
    guest_only = make_task("Only in guest data", local_dt(5, 17), 45)
    response = fe.sync({"tasks": {"upsert": [guest_version, guest_only]}})  # no delete lists, no special mode

    assert response.status_code == 200
    tasks = {t["id"]: t for t in fe.state(sync_google=False).json()["tasks"]}
    assert tasks[old["id"]]["title"] == "New title from my guest data" and tasks[old["id"]]["duration_min"] == 90
    assert tasks[account_only["id"]]["title"] == "Only in my account"
    assert tasks[guest_only["id"]]["title"] == "Only in guest data"


def test_a_sync_that_cannot_be_saved_saves_nothing_and_names_the_problem_items(signed_in):
    fe = signed_in()
    good = make_task("Fine", local_dt(3, 17), 60)
    orphan = {"id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()), "start": local_dt(3, 9).isoformat(),
              "end": local_dt(3, 10).isoformat(), "locked": False}

    response = fe.sync({"tasks": {"upsert": [good]}, "chunks": {"upsert": [orphan]}})

    assert response.status_code == 422
    assert response.json()["ids"] == [orphan["id"]] and isinstance(response.json()["detail"], str)
    assert fe.state(sync_google=False).json()["tasks"] == []  # the valid task was rolled back with it


def test_validation_errors_point_at_the_offending_item(signed_in):
    fe = signed_in()
    bad = {**make_task("Bad", local_dt(3, 17), 60), "duration_min": 0}
    response = fe.sync({"tasks": {"upsert": [bad]}})
    body = response.json()
    assert response.status_code == 422 and body["detail"] == "Validation failed"
    assert body["ids"] == [bad["id"]]
    assert body["errors"][0]["loc"][-1] == "duration_min"
    assert "input" not in body["errors"][0]  # submitted values are not echoed back


def test_sync_is_idempotent_so_a_retry_after_a_network_error_is_safe(signed_in):
    fe = signed_in()
    task = make_task("Retry me", local_dt(3, 17), 60)
    payload = {"tasks": {"upsert": [task]}, "settings": {"padding_min": 25}}
    for _ in range(3):
        assert fe.sync(payload).status_code == 200
    state = fe.state(sync_google=False).json()
    assert len(state["tasks"]) == 1 and state["settings"]["padding_min"] == 25


def test_the_schedule_endpoint_requires_a_timezone(frontend):
    body = {"tasks": [], "blocks": [], "settings": {"padding_min": 15}}
    response = frontend.schedule(body)
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "settings", "timezone"]


def test_health_check_needs_no_login(frontend):
    response = frontend.api("GET", "/", auth=False)
    assert response.status_code == 200 and response.json() == {"status": "ok"}
