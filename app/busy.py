"""Turn blocks and Google events into padded busy intervals for the scheduler.

- Recurring blocks are expanded with python-dateutil in the user's timezone, so a 9:00 class
  stays at 9:00 wall-clock across daylight-saving changes. The END of every occurrence also
  keeps its wall-clock time (a 23:00 to 07:00 sleep block ends at 07:00 on DST night too).
- Recurrence rules are restricted to a small grammar (see `parse_rrule`) so a crafted rule from
  a guest cannot make the server loop or burn CPU.
- Padding is added before and after each block/event, never around tasks.
- Calendar padding overrides replace the global padding for that calendar's events.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable, Iterable

from dateutil.rrule import rrulestr

from .scheduler import Interval

UTC = timezone.utc
MAX_OCCURRENCES_PER_BLOCK = 4000  # safety net; a supported rule yields at most one per day
MAX_RRULE_LENGTH = 500
MAX_INTERVAL = 52
MAX_COUNT = 3660

_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_ALLOWED_KEYS = frozenset({"FREQ", "INTERVAL", "BYDAY", "UNTIL", "COUNT", "WKST"})
_UNTIL = re.compile(r"^([0-9]{8})(?:T([0-9]{6})(Z)?)?$")
_INT = re.compile(r"^[0-9]{1,6}$")


class RecurrenceError(ValueError):
    """A recurring block could not be expanded safely. `block_id` names it when known."""

    def __init__(self, message: str, block_id: str | None = None):
        super().__init__(message)
        self.block_id = block_id


@dataclass(frozen=True)
class BlockIn:
    start: datetime
    end: datetime
    rrule: str | None = None
    id: str | None = None


@dataclass(frozen=True)
class EventIn:
    calendar_id: str
    start: datetime
    end: datetime
    dismissed: bool = False


# ------------------------------------------------------------------------ rule grammar

def parse_rrule(text: str) -> dict[str, str]:
    """Validate `text` against the supported grammar and return its parts (keys and values upper-cased).

    Supported: a single-line RRULE (optional "RRULE:" prefix, 500 characters at most) using each of
    FREQ (DAILY|WEEKLY, required), INTERVAL (1-52), BYDAY (weekly only), UNTIL or COUNT (not both),
    and WKST at most once. Raises ValueError for anything else.
    """
    body = text.strip()
    if not body:
        raise ValueError("rrule is empty")
    if len(body) > MAX_RRULE_LENGTH:
        raise ValueError(f"rrule is longer than {MAX_RRULE_LENGTH} characters")
    if body.upper().startswith("RRULE:"):
        body = body[6:]

    parts: dict[str, str] = {}
    for item in body.split(";"):
        key, sep, value = item.partition("=")
        key, value = key.strip().upper(), value.strip().upper()
        if not sep or not key or not value:
            raise ValueError("rrule must look like FREQ=WEEKLY;BYDAY=MO,WE")
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"rrule part {key} is not supported (allowed: {', '.join(sorted(_ALLOWED_KEYS))})")
        if key in parts:
            raise ValueError(f"rrule part {key} appears more than once")
        parts[key] = value

    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        raise ValueError("rrule FREQ must be DAILY or WEEKLY")

    if "INTERVAL" in parts:
        if not _INT.match(parts["INTERVAL"]) or not 1 <= int(parts["INTERVAL"]) <= MAX_INTERVAL:
            raise ValueError(f"rrule INTERVAL must be a whole number from 1 to {MAX_INTERVAL}")

    if "BYDAY" in parts:
        if freq != "WEEKLY":
            raise ValueError("rrule BYDAY is only supported with FREQ=WEEKLY")
        days = parts["BYDAY"].split(",")
        if any(d not in _WEEKDAYS for d in days) or len(set(days)) != len(days):
            raise ValueError("rrule BYDAY must be a list of distinct days from MO,TU,WE,TH,FR,SA,SU")

    if "WKST" in parts and parts["WKST"] not in _WEEKDAYS:
        raise ValueError("rrule WKST must be one of MO,TU,WE,TH,FR,SA,SU")

    if "UNTIL" in parts and "COUNT" in parts:
        raise ValueError("rrule cannot have both UNTIL and COUNT")
    if "COUNT" in parts:
        if not _INT.match(parts["COUNT"]) or not 1 <= int(parts["COUNT"]) <= MAX_COUNT:
            raise ValueError(f"rrule COUNT must be a whole number from 1 to {MAX_COUNT}")
    if "UNTIL" in parts:
        match = _UNTIL.match(parts["UNTIL"])
        if not match:
            raise ValueError("rrule UNTIL must look like 20261215, 20261215T235959 or 20261215T235959Z")
        try:
            datetime.strptime(match.group(1) + (match.group(2) or "000000"), "%Y%m%d%H%M%S")
        except ValueError as exc:
            raise ValueError("rrule UNTIL is not a real date") from exc
    return parts


def _dateutil_text(parts: dict[str, str], tz: tzinfo) -> str:
    """Rule text for dateutil, with UNTIL converted to a naive local time (dtstart is naive local)."""
    out = dict(parts)
    if "UNTIL" in out:
        match = _UNTIL.match(out["UNTIL"])
        date_part, time_part, zulu = match.groups()
        if time_part is None:  # a bare date includes that whole day
            out["UNTIL"] = f"{date_part}T235959"
        elif zulu:
            utc_dt = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            out["UNTIL"] = utc_dt.astimezone(tz).strftime("%Y%m%dT%H%M%S")
        else:
            out["UNTIL"] = f"{date_part}T{time_part}"
    return ";".join(f"{k}={v}" for k, v in out.items())


def validate_rrule(text: str) -> None:
    """Raise ValueError unless `text` is a supported rule that dateutil can expand."""
    parts = parse_rrule(text)
    try:
        rrulestr(_dateutil_text(parts, UTC), dtstart=datetime(2000, 1, 3))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"invalid rrule: {exc}") from exc


# --------------------------------------------------------------------------- expansion

def _fast_forward(dtstart: datetime, parts: dict[str, str], target: datetime) -> datetime:
    """Move dtstart forward by whole recurrence periods so dateutil need not walk from year 2000.

    The shift is a whole number of periods (INTERVAL days, or INTERVAL weeks), so weekdays and
    week alignment are unchanged. Not done with COUNT, which counts occurrences from the first.
    """
    if "COUNT" in parts or target <= dtstart:
        return dtstart
    interval = int(parts.get("INTERVAL", "1"))
    period = timedelta(days=interval * (7 if parts["FREQ"] == "WEEKLY" else 1))
    periods = (target - dtstart) // period - 1  # one period of margin
    return dtstart + periods * period if periods > 0 else dtstart


def expand_block(
    block: BlockIn,
    tz: tzinfo,
    lo: datetime,
    hi: datetime,
    pad: timedelta,
    check: Callable[[], None] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Occurrences of `block` that touch [lo, hi] once padded, as UTC intervals (unpadded).

    `check` is called periodically and may raise to abort (used for the compute time limit).
    """
    if not block.rrule:
        if block.end + pad > lo and block.start - pad < hi:
            return [(block.start, block.end)]
        return []

    parts = parse_rrule(block.rrule)
    first_start = block.start.astimezone(tz).replace(tzinfo=None)
    first_end = block.end.astimezone(tz).replace(tzinfo=None)
    # Length measured on the wall clock, so the END keeps its local time across DST changes.
    wall = first_end - first_start
    real = block.end - block.start
    if wall <= timedelta(0):  # the first occurrence itself straddles a clock change oddly
        wall = real

    window_lo = (lo - real - pad).astimezone(tz).replace(tzinfo=None) - timedelta(days=1)
    window_hi = (hi + pad).astimezone(tz).replace(tzinfo=None) + timedelta(days=1)
    dtstart = _fast_forward(first_start, parts, window_lo)
    rule = rrulestr(_dateutil_text(parts, tz), dtstart=dtstart)

    out: list[tuple[datetime, datetime]] = []
    for n, occ in enumerate(rule.xafter(window_lo, count=MAX_OCCURRENCES_PER_BLOCK + 1, inc=True)):
        if occ > window_hi:
            break
        if n >= MAX_OCCURRENCES_PER_BLOCK:
            raise RecurrenceError(
                f"A recurring block expands to more than {MAX_OCCURRENCES_PER_BLOCK} occurrences.", block.id
            )
        if check is not None and n % 64 == 0:
            check()
        start = occ.replace(tzinfo=tz).astimezone(UTC)
        end = (occ + wall).replace(tzinfo=tz).astimezone(UTC)
        if end <= start:
            end = start + real
        out.append((start, end))
    return out


def build_busy(
    blocks: Iterable[BlockIn],
    events: Iterable[EventIn],
    *,
    tz: tzinfo,
    global_padding_min: int,
    calendar_padding: dict[str, int | None],
    lo: datetime,
    hi: datetime,
    check: Callable[[], None] | None = None,
) -> list[Interval]:
    """Padded busy intervals touching [lo, hi]. Dismissed events are skipped."""
    out: list[Interval] = []
    gpad = timedelta(minutes=global_padding_min)
    for block in blocks:
        if check is not None:
            check()
        for s, e in expand_block(block, tz, lo, hi, gpad, check):
            out.append(Interval(s - gpad, e + gpad))
    for ev in events:
        if ev.dismissed:
            continue
        override = calendar_padding.get(ev.calendar_id)
        pad = timedelta(minutes=global_padding_min if override is None else override)
        if ev.end + pad > lo and ev.start - pad < hi:
            out.append(Interval(ev.start - pad, ev.end + pad))
    return out
