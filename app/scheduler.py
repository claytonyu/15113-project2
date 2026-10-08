"""Schedule generation engine.

Pure functions only: no database, no network, no clock. Everything is passed in, which keeps
the logic easy to test. All datetimes are timezone-aware.

Rules implemented here (see SPEC.md, "Schedule generation"):
- Tasks go only into free time: inside working hours, outside blocks (already padded by the
  caller), outside locked chunks, and before the task's deadline.
- Tasks are handled earliest deadline first.
- Every chunk STARTS on a 15-minute boundary (epoch-aligned). Blocks and padding are not
  snapped, so a chunk never overlaps an event by even a minute. Chunk LENGTHS are exact minutes.
- Nothing is scheduled before `now` rounded up to the next 15 minutes.
- `front_load`: earliest slots first. `even`: spread a task over the days available.
- A splittable task that only partly fits gets the part that fits; the rest is reported.
  A non-splittable task is placed whole or not at all.
- Locked chunks are fixed, occupy time, and count toward their task's duration.
"""
from __future__ import annotations

import time as _time
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Callable, Iterator

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRID_MIN = 15
GRID = timedelta(minutes=GRID_MIN)
MAX_HORIZON = timedelta(days=400)
DEFAULT_MIN_CHUNK_MIN = GRID_MIN


# --------------------------------------------------------------------------- data types

@dataclass(frozen=True)
class Task:
    id: str
    title: str
    due_at: datetime
    duration_min: int
    splittable: bool = False
    min_chunk_min: int | None = None


@dataclass(frozen=True)
class Chunk:
    id: str
    task_id: str
    start: datetime
    end: datetime
    locked: bool = False


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Options:
    tz: tzinfo
    work_start: time
    work_end: time
    spread_mode: str = "even"  # "even" | "front_load"


@dataclass(frozen=True)
class Missing:
    task_id: str
    missing_min: int


@dataclass(frozen=True)
class Warn:
    code: str
    message: str
    task_id: str | None = None
    chunk_id: str | None = None


@dataclass
class Result:
    chunks: list[Chunk] = field(default_factory=list)
    unschedulable: list[Missing] = field(default_factory=list)
    warnings: list[Warn] = field(default_factory=list)


class ScheduleTimeout(Exception):
    """Raised when generation exceeds its compute time limit."""


# ------------------------------------------------------------------------------ helpers

def snap_up(dt: datetime) -> datetime:
    """Round up to the next 15-minute boundary (no-op if already on one)."""
    n = -((-(dt - EPOCH)) // GRID)
    return EPOCH + n * GRID


def _minutes(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def subtract_intervals(
    windows: list[tuple[datetime, datetime]], busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """windows minus busy. Both sorted and non-overlapping (busy must be merged)."""
    out: list[tuple[datetime, datetime]] = []
    j = 0
    for ws, we in windows:
        cur = ws
        while j < len(busy) and busy[j][1] <= cur:
            j += 1
        k = j
        while k < len(busy) and busy[k][0] < we:
            bs, be = busy[k]
            if bs > cur:
                out.append((cur, bs))
            if be > cur:
                cur = be
            if cur >= we:
                break
            k += 1
        if cur < we:
            out.append((cur, we))
    return out


class FreeTime:
    """Sorted, disjoint free segments whose start times sit on the 15-minute grid."""

    def __init__(self, segments: list[tuple[datetime, datetime]]):
        self.segs = list(segments)

    def iter_range(self, lo: datetime, hi: datetime) -> Iterator[tuple[datetime, datetime]]:
        """Segments clipped to [lo, hi), starts snapped up to the grid, empty ones skipped."""
        segs = self.segs
        i = bisect_left(segs, lo, key=lambda seg: seg[1])
        while i < len(segs) and segs[i][0] < hi:
            s, e = segs[i]
            s = snap_up(max(s, lo))
            e = min(e, hi)
            if e > s:
                yield s, e
            i += 1

    def occupy(self, start: datetime, end: datetime) -> None:
        i = bisect_right(self.segs, start, key=lambda seg: seg[0]) - 1
        s, e = self.segs[i]
        if not (s <= start and end <= e):
            raise AssertionError("placement outside free segment")
        pieces = []
        if start > s:
            pieces.append((s, start))
        right = snap_up(end)
        if right < e:
            pieces.append((right, e))
        self.segs[i : i + 1] = pieces


def _choose_take(want: int, seg_min: int, remaining: int, min_chunk: int) -> int:
    """How many minutes to put in one segment, honoring the minimum chunk size.

    A chunk may be smaller than `min_chunk` only when it finishes the task. Otherwise we
    never leave a remainder smaller than `min_chunk` behind.
    """
    take = min(want, seg_min, remaining)
    if take <= 0:
        return 0
    if take == remaining:
        return take
    if remaining - take < min_chunk:
        take = remaining - min_chunk
    return take if take >= min_chunk else 0


# ---------------------------------------------------------------------- per-task placement

def _fill_front(local: FreeTime, remaining: int, min_chunk: int, tick: Callable[[], None]):
    placements: list[tuple[datetime, datetime]] = []
    for s, e in list(local.segs):
        if remaining <= 0:
            break
        tick()
        take = _choose_take(remaining, _minutes(e - s), remaining, min_chunk)
        if take:
            end = s + timedelta(minutes=take)
            placements.append((s, end))
            local.occupy(s, end)
            remaining -= take
    return placements, remaining


def _fill_even(local: FreeTime, remaining: int, min_chunk: int, tz: tzinfo, tick: Callable[[], None]):
    by_day: dict[date, list[tuple[datetime, datetime]]] = {}
    for s, e in local.segs:
        if _minutes(e - s) >= min_chunk:
            by_day.setdefault(s.astimezone(tz).date(), []).append((s, e))
    days = sorted(by_day)
    placements: list[tuple[datetime, datetime]] = []
    for idx, day in enumerate(days):
        if remaining <= 0:
            break
        target = _ceil_div(remaining, len(days) - idx)
        target = _ceil_div(target, GRID_MIN) * GRID_MIN
        day_left = min(max(target, min_chunk), remaining)
        for s, e in by_day[day]:
            if day_left <= 0 or remaining <= 0:
                break
            tick()
            take = _choose_take(min(day_left, remaining), _minutes(e - s), remaining, min_chunk)
            if take:
                end = s + timedelta(minutes=take)
                placements.append((s, end))
                local.occupy(s, end)
                remaining -= take
                day_left -= take
    if remaining > 0:
        # Some days had less room than their share (or rounding left a gap): fill the rest early.
        more, remaining = _fill_front(local, remaining, min_chunk, tick)
        placements.extend(more)
    return placements, remaining


def _place_whole(
    cands: list[tuple[datetime, datetime]],
    duration: int,
    spread_mode: str,
    tz: tzinfo,
    day_load: dict[date, int],
    tick: Callable[[], None],
):
    """Pick one slot for a non-splittable task, or None if nothing is long enough."""
    best = None
    best_key = None
    for s, e in cands:
        tick()
        if _minutes(e - s) < duration:
            continue
        if spread_mode == "front_load":
            best = s
            break
        key = (day_load.get(s.astimezone(tz).date(), 0), s)
        if best_key is None or key < best_key:
            best_key, best = key, s
    if best is None:
        return None
    return best, best + timedelta(minutes=duration)


# --------------------------------------------------------------------------- entry point

def schedule(
    tasks: list[Task],
    locked_chunks: list[Chunk],
    busy: list[Interval],
    options: Options,
    now: datetime,
    *,
    time_limit_s: float | None = None,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> Result:
    """Generate unlocked chunks for every task. `busy` must already include padding."""
    deadline = _time.monotonic() + time_limit_s if time_limit_s else None

    def tick() -> None:
        if deadline is not None and _time.monotonic() > deadline:
            raise ScheduleTimeout()

    tz = options.tz
    result = Result()
    task_by_id = {t.id: t for t in tasks}
    busy_merged = merge_intervals([(b.start, b.end) for b in busy])

    # --- locked chunks: validate, warn, and total them per task
    locked_ok: list[Chunk] = []
    locked_min: dict[str, int] = {}
    day_load: dict[date, int] = {}
    for c in sorted(locked_chunks, key=lambda c: (c.start, c.id)):
        task = task_by_id.get(c.task_id)
        if task is None:
            result.warnings.append(
                Warn("orphan_locked_chunk", "A locked chunk belongs to a task that no longer exists and was dropped.",
                     task_id=c.task_id, chunk_id=c.id)
            )
            continue
        locked_ok.append(c)
        mins = _minutes(c.end - c.start)
        locked_min[c.task_id] = locked_min.get(c.task_id, 0) + mins
        d = c.start.astimezone(tz).date()
        day_load[d] = day_load.get(d, 0) + mins
        if c.start < now:
            result.warnings.append(
                Warn("locked_chunk_in_past", f"Locked chunk for '{task.title}' starts in the past.",
                     task_id=c.task_id, chunk_id=c.id)
            )
        if c.end > task.due_at:
            result.warnings.append(
                Warn("locked_chunk_after_deadline", f"Locked chunk for '{task.title}' ends after its deadline.",
                     task_id=c.task_id, chunk_id=c.id)
            )
        i = bisect_left(busy_merged, c.start, key=lambda b: b[1])
        if i < len(busy_merged) and busy_merged[i][0] < c.end and busy_merged[i][1] > c.start:
            result.warnings.append(
                Warn("locked_chunk_overlaps_block",
                     f"Locked chunk for '{task.title}' overlaps a block or its padding.",
                     task_id=c.task_id, chunk_id=c.id)
            )

    # --- what still needs placing
    needs: dict[str, int] = {}
    for t in tasks:
        need = t.duration_min - locked_min.get(t.id, 0)
        if need > 0:
            needs[t.id] = need
    pending = sorted(
        (t for t in tasks if t.id in needs),
        key=lambda t: (t.due_at, -t.duration_min, t.title, t.id),
    )
    if not pending:
        return result

    # --- build free time between lo and hi
    lo = snap_up(now)
    hi = min(max(t.due_at for t in pending), lo + MAX_HORIZON)
    for t in pending:
        if t.due_at > hi:
            result.warnings.append(
                Warn("deadline_beyond_horizon",
                     f"'{t.title}' is due more than {MAX_HORIZON.days} days away; only the nearer part was planned.",
                     task_id=t.id)
            )

    windows: list[tuple[datetime, datetime]] = []
    if hi > lo:
        day = lo.astimezone(tz).date()
        last_day = hi.astimezone(tz).date()
        while day <= last_day:
            tick()
            ws = max(datetime.combine(day, options.work_start, tzinfo=tz).astimezone(UTC), lo)
            we = min(datetime.combine(day, options.work_end, tzinfo=tz).astimezone(UTC), hi)
            if we > ws:
                windows.append((ws, we))
            day += timedelta(days=1)
    locked_intervals = [(c.start, c.end) for c in locked_ok]
    all_busy = merge_intervals(list(busy_merged) + locked_intervals)
    free_segments = []
    for s, e in subtract_intervals(windows, all_busy):
        s = snap_up(s)
        if e > s:
            free_segments.append((s, e))
    free = FreeTime(free_segments)

    # --- place tasks, earliest deadline first
    for task in pending:
        need = needs[task.id]
        if task.due_at <= lo:
            result.warnings.append(
                Warn("deadline_passed", f"'{task.title}' is already past due, so nothing could be scheduled.",
                     task_id=task.id)
            )
            result.unschedulable.append(Missing(task.id, need))
            continue

        cands = list(free.iter_range(lo, task.due_at))
        placements: list[tuple[datetime, datetime]] = []
        remaining = need
        if task.splittable:
            min_chunk = min(task.min_chunk_min or DEFAULT_MIN_CHUNK_MIN, need)
            local = FreeTime(cands)
            if options.spread_mode == "front_load":
                placements, remaining = _fill_front(local, need, min_chunk, tick)
            else:
                placements, remaining = _fill_even(local, need, min_chunk, tz, tick)
        else:
            slot = _place_whole(cands, need, options.spread_mode, tz, day_load, tick)
            if slot is not None:
                placements, remaining = [slot], 0

        for s, e in placements:
            free.occupy(s, e)
            d = s.astimezone(tz).date()
            day_load[d] = day_load.get(d, 0) + _minutes(e - s)
            result.chunks.append(Chunk(new_id(), task.id, s, e, locked=False))
        if remaining > 0:
            result.unschedulable.append(Missing(task.id, remaining))

    result.chunks.sort(key=lambda c: (c.start, c.task_id))
    return result
