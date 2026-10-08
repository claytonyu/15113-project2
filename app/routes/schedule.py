"""Endpoint 6: generate a schedule."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auth, repo, scheduler, services
from ..busy import BlockIn, EventIn, RecurrenceError, build_busy
from ..config import get_config
from ..db import connect
from ..limiter import limiter
from ..models import Chunk, Event, ScheduleRequest, ScheduleResponse, UnschedulableItem, WarningItem

router = APIRouter(tags=["schedule"])


def _hhmm(value: str):
    hour, minute = value.split(":")
    return datetime(2000, 1, 1, int(hour), int(minute)).time()


@router.post("/schedule", response_model=ScheduleResponse, summary="Generate unlocked chunks for the given tasks")
@limiter.limit(lambda: get_config().schedule_rate_limit)
def generate_schedule(request: Request, body: ScheduleRequest, user: dict | None = Depends(auth.optional_user)):
    """Stateless: never writes to the database. Guests never touch it at all.

    Logged-in users additionally get their selected Google calendars (minus dismissed events)
    treated as blocks. The client saves the result through PATCH /sync.
    """
    cfg = get_config()
    now = body.now or datetime.now(timezone.utc)
    settings = body.settings
    tz = ZoneInfo(settings.timezone)

    tasks = [
        scheduler.Task(str(t.id), t.title, t.due_at, t.duration_min, t.splittable, t.min_chunk_min)
        for t in body.tasks
    ]
    locked = [scheduler.Chunk(str(c.id), str(c.task_id), c.start, c.end, True) for c in body.locked_chunks]
    horizon_end = min(max((t.due_at for t in tasks), default=now), now + scheduler.MAX_HORIZON)

    # --- Google events (logged-in users only)
    events: list[dict] = []
    google_error: str | None = None
    calendar_padding: dict[str, int | None] = {}
    if user is not None:
        with connect() as conn:
            stored = {c["id"]: c for c in repo.fetch_calendars(conn, user["id"])}
        for patch in body.calendars or []:  # unsaved selection/padding from the client wins
            if patch.id in stored:
                if patch.selected is not None:
                    stored[patch.id]["selected"] = patch.selected
                if "padding_override_min" in patch.model_fields_set:
                    stored[patch.id]["padding_override_min"] = patch.padding_override_min
        calendar_padding = {cid: c["padding_override_min"] for cid, c in stored.items()}
        selected = [cid for cid, c in stored.items() if c["selected"]]
        if selected:
            access, google_error = services.google_access(user["id"])
            if access:
                events, google_error = services.fetch_user_events(user["id"], access, selected, now, horizon_end, tz)

    # The compute limit covers recurrence expansion and placement (not the wait on Google above).
    deadline_at = time.monotonic() + cfg.schedule_timeout_s

    def check_time() -> None:
        if time.monotonic() > deadline_at:
            raise scheduler.ScheduleTimeout()

    options = scheduler.Options(
        tz=tz,
        work_start=_hhmm(settings.work_start),
        work_end=_hhmm(settings.work_end),
        spread_mode=settings.spread_mode,
    )
    try:
        busy = build_busy(
            [BlockIn(b.start, b.end, b.rrule, str(b.id)) for b in body.blocks],
            [EventIn(e["calendar_id"], e["start"], e["end"], e.get("dismissed", False)) for e in events],
            tz=tz,
            global_padding_min=settings.padding_min,
            calendar_padding=calendar_padding,
            lo=now,
            hi=horizon_end,
            check=check_time,
        )
        result = scheduler.schedule(tasks, locked, busy, options, now, deadline_at=deadline_at)
    except scheduler.ScheduleTimeout:
        raise HTTPException(422, "This schedule is too large to compute in time. Try fewer tasks or blocks.")
    except RecurrenceError as exc:
        ids = [exc.block_id] if exc.block_id else []
        return JSONResponse(status_code=422, content={"detail": str(exc), "ids": ids})

    return ScheduleResponse(
        chunks=[Chunk(id=UUID(c.id), task_id=UUID(c.task_id), start=c.start, end=c.end, locked=False) for c in result.chunks],
        unschedulable=[UnschedulableItem(task_id=UUID(m.task_id), missing_min=m.missing_min) for m in result.unschedulable],
        google_events=[Event(**e) for e in events],
        warnings=[
            WarningItem(
                code=w.code,
                message=w.message,
                task_id=UUID(w.task_id) if w.task_id else None,
                chunk_id=UUID(w.chunk_id) if w.chunk_id else None,
            )
            for w in result.warnings
        ],
        google_error=google_error,
    )
