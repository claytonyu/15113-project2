"""Scheduler tests (pure logic; no database or network)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.busy import BlockIn, EventIn, build_busy, validate_rrule
from app.scheduler import (
    Chunk,
    Interval,
    Options,
    ScheduleTimeout,
    Task,
    schedule,
    snap_up,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def dt(y, mo, d, h=0, mi=0, tz=UTC):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


def opts(mode="front_load", tz=UTC, ws=time(8), we=time(22)):
    return Options(tz=tz, work_start=ws, work_end=we, spread_mode=mode)


def ids():
    n = iter(range(10_000))
    return lambda: f"c{next(n)}"


def run(tasks, now, *, busy=(), locked=(), options=None, **kw):
    return schedule(list(tasks), list(locked), list(busy), options or opts(), now, new_id=ids(), **kw)


def total_min(chunks, task_id=None):
    return sum(
        int((c.end - c.start).total_seconds() // 60)
        for c in chunks
        if task_id is None or c.task_id == task_id
    )


def assert_valid(result, tasks, busy=(), options=None):
    """Invariants every schedule must satisfy."""
    o = options or opts()
    due = {t.id: t.due_at for t in tasks}
    for c in result.chunks:
        assert c.start.minute % 15 == 0 and c.start.second == 0, "chunk must start on the 15-minute grid"
        assert c.end > c.start
        assert c.end <= due[c.task_id], "chunk must end by the deadline"
        ls, le = c.start.astimezone(o.tz), c.end.astimezone(o.tz)
        assert ls.date() == le.date()
        assert o.work_start <= ls.time() and (le.time() <= o.work_end), "chunk must be inside working hours"
        for b in busy:
            assert c.end <= b.start or c.start >= b.end, "chunk must not overlap busy time"
    ordered = sorted(result.chunks, key=lambda c: c.start)
    for a, b in zip(ordered, ordered[1:]):
        assert a.end <= b.start, "chunks must not overlap"


# ------------------------------------------------------------------ basics

def test_snap_up():
    assert snap_up(dt(2026, 1, 5, 14, 7)) == dt(2026, 1, 5, 14, 15)
    assert snap_up(dt(2026, 1, 5, 14, 15)) == dt(2026, 1, 5, 14, 15)
    assert snap_up(dt(2026, 1, 5, 14, 0) + timedelta(seconds=1)) == dt(2026, 1, 5, 14, 15)


def test_single_task_front_load_starts_at_now_rounded_up():
    t = Task("a", "Essay", dt(2026, 1, 6, 12), 60)
    r = run([t], dt(2026, 1, 5, 14, 7))
    assert [(c.start, c.end) for c in r.chunks] == [(dt(2026, 1, 5, 14, 15), dt(2026, 1, 5, 15, 15))]
    assert r.unschedulable == []
    assert_valid(r, [t])


def test_nothing_scheduled_before_now_or_outside_working_hours():
    t = Task("a", "Late", dt(2026, 1, 6, 12), 60)
    r = run([t], dt(2026, 1, 5, 23, 0))  # after working hours: starts next morning
    assert r.chunks[0].start == dt(2026, 1, 6, 8)


def test_respects_deadline_and_reports_missing():
    t = Task("a", "Too big", dt(2026, 1, 5, 10), 300, splittable=True, min_chunk_min=30)
    r = run([t], dt(2026, 1, 5, 8))
    assert total_min(r.chunks) == 120
    assert [(m.task_id, m.missing_min) for m in r.unschedulable] == [("a", 180)]
    assert_valid(r, [t])


def test_non_splittable_is_all_or_nothing():
    t = Task("a", "Block of 3h", dt(2026, 1, 5, 10), 180)
    r = run([t], dt(2026, 1, 5, 8))
    assert r.chunks == []
    assert r.unschedulable[0].missing_min == 180


def test_non_splittable_fits_in_gap_between_blocks():
    t = Task("a", "Two hours", dt(2026, 1, 5, 20), 120)
    busy = [Interval(dt(2026, 1, 5, 8), dt(2026, 1, 5, 10)), Interval(dt(2026, 1, 5, 11), dt(2026, 1, 5, 18))]
    r = run([t], dt(2026, 1, 5, 7), busy=busy)
    assert (r.chunks[0].start, r.chunks[0].end) == (dt(2026, 1, 5, 18), dt(2026, 1, 5, 20))
    assert_valid(r, [t], busy)


def test_past_due_task_is_flagged():
    t = Task("a", "Late", dt(2026, 1, 5, 9), 60)
    r = run([t], dt(2026, 1, 5, 10))
    assert r.chunks == []
    assert r.unschedulable[0].missing_min == 60
    assert any(w.code == "deadline_passed" for w in r.warnings)


def test_other_tasks_still_scheduled_when_one_is_impossible():
    bad = Task("bad", "Impossible", dt(2026, 1, 5, 9), 600)
    ok = Task("ok", "Fine", dt(2026, 1, 7, 12), 60)
    r = run([bad, ok], dt(2026, 1, 5, 8))
    assert total_min(r.chunks, "ok") == 60
    assert {m.task_id for m in r.unschedulable} == {"bad"}


# ---------------------------------------------------- blocks, padding, working hours

def test_chunks_avoid_busy_time_to_the_minute():
    t = Task("a", "Work", dt(2026, 1, 5, 20), 120, splittable=True, min_chunk_min=15)
    busy = [Interval(dt(2026, 1, 5, 9, 7), dt(2026, 1, 5, 11, 22))]
    r = run([t], dt(2026, 1, 5, 8), busy=busy)
    assert_valid(r, [t], busy)
    # 8:00-9:07 is free (67 min); next free start after 11:22 snaps to 11:30
    assert [(c.start, c.end) for c in r.chunks] == [
        (dt(2026, 1, 5, 8), dt(2026, 1, 5, 9, 7)),
        (dt(2026, 1, 5, 11, 30), dt(2026, 1, 5, 12, 23)),
    ]


def test_all_chunks_start_on_grid_even_with_odd_durations():
    t = Task("a", "Odd", dt(2026, 1, 5, 20), 100, splittable=True, min_chunk_min=10)
    busy = [Interval(dt(2026, 1, 5, 8, 33), dt(2026, 1, 5, 9, 1)), Interval(dt(2026, 1, 5, 9, 40), dt(2026, 1, 5, 9, 50))]
    r = run([t], dt(2026, 1, 5, 8), busy=busy)
    assert total_min(r.chunks) == 100
    assert_valid(r, [t], busy)


def test_working_hours_are_respected():
    t = Task("a", "Work", dt(2026, 1, 7, 23), 600, splittable=True, min_chunk_min=60)
    o = opts(ws=time(10), we=time(12))
    r = run([t], dt(2026, 1, 5, 0), options=o)
    assert_valid(r, [t], options=o)
    assert total_min(r.chunks) == 360  # 3 days x 2h window... Jan 5, 6, 7
    assert r.unschedulable[0].missing_min == 240


def test_padding_via_build_busy_keeps_tasks_away_from_blocks():
    block = BlockIn(dt(2026, 1, 5, 12), dt(2026, 1, 5, 13))
    busy = build_busy([block], [], tz=UTC, global_padding_min=30, calendar_padding={},
                      lo=dt(2026, 1, 5), hi=dt(2026, 1, 6))
    assert busy == [Interval(dt(2026, 1, 5, 11, 30), dt(2026, 1, 5, 13, 30))]
    t = Task("a", "Work", dt(2026, 1, 5, 18), 240, splittable=True, min_chunk_min=30)
    r = run([t], dt(2026, 1, 5, 8), busy=busy)
    assert_valid(r, [t], busy)
    assert r.chunks[0].end <= dt(2026, 1, 5, 11, 30)
    assert min(c.start for c in r.chunks if c.start > dt(2026, 1, 5, 12)) >= dt(2026, 1, 5, 13, 30)


def test_calendar_padding_override_replaces_global_padding():
    ev = EventIn("cal1", dt(2026, 1, 5, 12), dt(2026, 1, 5, 13))
    busy = build_busy([], [ev], tz=UTC, global_padding_min=30, calendar_padding={"cal1": 0},
                      lo=dt(2026, 1, 5), hi=dt(2026, 1, 6))
    assert busy == [Interval(dt(2026, 1, 5, 12), dt(2026, 1, 5, 13))]


def test_dismissed_events_are_ignored():
    ev = EventIn("cal1", dt(2026, 1, 5, 12), dt(2026, 1, 5, 13), dismissed=True)
    assert build_busy([], [ev], tz=UTC, global_padding_min=15, calendar_padding={},
                      lo=dt(2026, 1, 5), hi=dt(2026, 1, 6)) == []


def test_recurring_block_expands_daily_and_weekly():
    weekly = BlockIn(dt(2026, 1, 5, 9), dt(2026, 1, 5, 10), "FREQ=WEEKLY;BYDAY=MO,WE")
    busy = build_busy([weekly], [], tz=UTC, global_padding_min=0, calendar_padding={},
                      lo=dt(2026, 1, 5), hi=dt(2026, 1, 20))
    starts = sorted(b.start for b in busy)
    assert starts == [dt(2026, 1, 5, 9), dt(2026, 1, 7, 9), dt(2026, 1, 12, 9), dt(2026, 1, 14, 9), dt(2026, 1, 19, 9)]


def test_recurring_block_keeps_wall_clock_time_across_dst():
    # 9:00 New York time every day, across the March 2026 DST change (Mar 8).
    start = dt(2026, 3, 6, 9, tz=NY)
    block = BlockIn(start, start + timedelta(hours=1), "FREQ=DAILY;COUNT=5")
    busy = build_busy([block], [], tz=NY, global_padding_min=0, calendar_padding={},
                      lo=dt(2026, 3, 5), hi=dt(2026, 3, 20))
    local_hours = {b.start.astimezone(NY).hour for b in busy}
    assert local_hours == {9}
    assert len(busy) == 5


def test_recurrence_until_is_honored():
    block = BlockIn(dt(2026, 1, 5, 9), dt(2026, 1, 5, 10), "FREQ=DAILY;UNTIL=20260107T235959Z")
    busy = build_busy([block], [], tz=UTC, global_padding_min=0, calendar_padding={},
                      lo=dt(2026, 1, 1), hi=dt(2026, 2, 1))
    assert len(busy) == 3


@pytest.mark.parametrize("bad", ["FREQ=MONTHLY", "FREQ=YEARLY", "DTSTART:20260101\nFREQ=DAILY", "nonsense", ""])
def test_validate_rrule_rejects_unsupported(bad):
    with pytest.raises(ValueError):
        validate_rrule(bad)


def test_validate_rrule_accepts_supported():
    validate_rrule("FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20260501T000000Z")
    validate_rrule("RRULE:FREQ=DAILY;COUNT=10")


# ----------------------------------------------------------------- ordering and modes

def test_earliest_deadline_first_gets_the_early_slot():
    early = Task("early", "Soon", dt(2026, 1, 5, 12), 120)
    late = Task("late", "Later", dt(2026, 1, 9, 12), 120)
    r = run([late, early], dt(2026, 1, 5, 8))
    by = {c.task_id: c for c in r.chunks}
    assert by["early"].start == dt(2026, 1, 5, 8)
    assert by["late"].start == dt(2026, 1, 5, 10)


def test_front_load_packs_earliest_slots():
    t = Task("a", "Long", dt(2026, 1, 9, 12), 600, splittable=True, min_chunk_min=30)
    r = run([t], dt(2026, 1, 5, 8))
    assert {c.start.date() for c in r.chunks} == {dt(2026, 1, 5).date()}
    assert total_min(r.chunks) == 600


def test_even_mode_spreads_across_days():
    t = Task("a", "Long", dt(2026, 1, 8, 22), 600, splittable=True, min_chunk_min=30)  # Jan 5-8: 4 days
    r = run([t], dt(2026, 1, 5, 8), options=opts("even"))
    assert_valid(r, [t], options=opts("even"))
    per_day = {}
    for c in r.chunks:
        per_day[c.start.date()] = per_day.get(c.start.date(), 0) + total_min([c])
    assert len(per_day) == 4
    assert sum(per_day.values()) == 600
    assert max(per_day.values()) - min(per_day.values()) <= 60


def test_even_mode_skips_days_without_room_and_still_finishes():
    t = Task("a", "Work", dt(2026, 1, 7, 22), 360, splittable=True, min_chunk_min=30)
    busy = [Interval(dt(2026, 1, 6, 0), dt(2026, 1, 7, 0))]  # Jan 6 fully busy
    r = run([t], dt(2026, 1, 5, 8), busy=busy, options=opts("even"))
    assert total_min(r.chunks) == 360
    assert dt(2026, 1, 6).date() not in {c.start.date() for c in r.chunks}
    assert_valid(r, [t], busy, opts("even"))


def test_even_mode_falls_back_when_later_days_are_small():
    # Day 2 has only 30 free minutes; the shortfall must not make the task unschedulable.
    t = Task("a", "Work", dt(2026, 1, 6, 22), 480, splittable=True, min_chunk_min=30)
    busy = [Interval(dt(2026, 1, 6, 8, 30), dt(2026, 1, 6, 22))]
    r = run([t], dt(2026, 1, 5, 8), busy=busy, options=opts("even"))
    assert total_min(r.chunks) == 480
    assert r.unschedulable == []
    assert_valid(r, [t], busy, opts("even"))


def test_even_mode_non_splittable_goes_to_least_loaded_day():
    a = Task("a", "A", dt(2026, 1, 8, 22), 120)
    b = Task("b", "B", dt(2026, 1, 8, 23), 120)
    r = run([a, b], dt(2026, 1, 5, 8), options=opts("even"))
    assert len({c.start.date() for c in r.chunks}) == 2


# --------------------------------------------------------------- min chunk handling

def test_min_chunk_never_leaves_tiny_remainder():
    t = Task("a", "Work", dt(2026, 1, 5, 20), 70, splittable=True, min_chunk_min=30)
    busy = [Interval(dt(2026, 1, 5, 8, 45), dt(2026, 1, 5, 20))]  # only 45 free at start
    r = run([t], dt(2026, 1, 5, 8), busy=busy)
    sizes = sorted(total_min([c]) for c in r.chunks)
    assert all(s >= 30 for s in sizes) or sum(sizes) == 70
    assert all(s >= 30 for s in sizes[:-1])


def test_chunk_smaller_than_min_allowed_only_to_finish_task():
    t = Task("a", "Tiny", dt(2026, 1, 5, 20), 10, splittable=True, min_chunk_min=30)
    r = run([t], dt(2026, 1, 5, 8))
    assert total_min(r.chunks) == 10


# ------------------------------------------------------------------- locked chunks

def test_locked_chunks_count_toward_duration_and_stay_fixed():
    t = Task("a", "Work", dt(2026, 1, 5, 20), 180, splittable=True, min_chunk_min=30)
    locked = [Chunk("L1", "a", dt(2026, 1, 5, 9), dt(2026, 1, 5, 10), locked=True)]
    r = run([t], dt(2026, 1, 5, 8), locked=locked)
    assert total_min(r.chunks) == 120  # only the remaining 2h is proposed
    assert all(c.id != "L1" for c in r.chunks)
    for c in r.chunks:
        assert c.end <= dt(2026, 1, 5, 9) or c.start >= dt(2026, 1, 5, 10), "must not overlap the locked chunk"


def test_fully_locked_task_gets_no_new_chunks():
    t = Task("a", "Done", dt(2026, 1, 5, 20), 60)
    locked = [Chunk("L1", "a", dt(2026, 1, 5, 9), dt(2026, 1, 5, 10), locked=True)]
    r = run([t], dt(2026, 1, 5, 8), locked=locked)
    assert r.chunks == [] and r.unschedulable == []


def test_locked_chunk_warnings():
    t = Task("a", "Work", dt(2026, 1, 5, 12), 60)
    past = Chunk("p", "a", dt(2026, 1, 5, 6), dt(2026, 1, 5, 7), locked=True)
    late = Chunk("l", "a", dt(2026, 1, 5, 13), dt(2026, 1, 5, 14), locked=True)
    orphan = Chunk("o", "ghost", dt(2026, 1, 5, 15), dt(2026, 1, 5, 16), locked=True)
    clash = Chunk("x", "a", dt(2026, 1, 5, 17), dt(2026, 1, 5, 18), locked=True)
    busy = [Interval(dt(2026, 1, 5, 17, 30), dt(2026, 1, 5, 19))]
    r = run([t], dt(2026, 1, 5, 8), locked=[past, late, orphan, clash], busy=busy)
    codes = {(w.code, w.chunk_id) for w in r.warnings}
    assert ("locked_chunk_in_past", "p") in codes
    assert ("locked_chunk_after_deadline", "l") in codes
    assert ("orphan_locked_chunk", "o") in codes
    assert ("locked_chunk_overlaps_block", "x") in codes


def test_locked_chunk_outside_working_hours_is_kept_and_blocks_nothing_extra():
    t = Task("a", "Work", dt(2026, 1, 5, 20), 60)
    locked = [Chunk("L", "a", dt(2026, 1, 5, 23), dt(2026, 1, 6, 0), locked=True)]
    r = run([t], dt(2026, 1, 5, 8), locked=locked)
    assert r.chunks == []


# ------------------------------------------------------------------ time zones, limits

def test_working_hours_follow_the_user_timezone():
    o = opts(tz=NY)
    t = Task("a", "Work", dt(2026, 1, 7, 12), 60)
    r = run([t], dt(2026, 1, 5, 12), options=o)  # 7:00 New York
    assert r.chunks[0].start == dt(2026, 1, 5, 8, tz=NY).astimezone(UTC)
    assert_valid(r, [t], options=o)


def test_working_hours_stay_at_local_wall_clock_across_dst():
    o = opts(tz=NY)
    t = Task("a", "Work", dt(2026, 3, 10, 12), 600, splittable=True, min_chunk_min=60)
    r = run([t], dt(2026, 3, 7, 12), options=o)  # DST starts Mar 8, 2026
    assert_valid(r, [t], options=o)
    assert {c.start.astimezone(NY).hour >= 8 for c in r.chunks} == {True}


def test_time_limit_raises():
    tasks = [Task(f"t{i}", "x", dt(2026, 12, 30), 600, splittable=True, min_chunk_min=15) for i in range(50)]
    with pytest.raises(ScheduleTimeout):
        run(tasks, dt(2026, 1, 1), time_limit_s=1e-9)


def test_many_tasks_finish_quickly():
    import time as _t

    tasks = [Task(f"t{i}", f"T{i}", dt(2026, 3, 1) + timedelta(hours=i), 90, splittable=True, min_chunk_min=30) for i in range(200)]
    started = _t.monotonic()
    r = run(tasks, dt(2026, 1, 1), options=opts("even"))
    assert _t.monotonic() - started < 5
    assert_valid(r, tasks, options=opts("even"))
