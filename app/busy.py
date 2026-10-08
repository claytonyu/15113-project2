"""Turn blocks and Google events into padded busy intervals for the scheduler.

- Recurring blocks are expanded with python-dateutil in the user's timezone, so a 9:00 class
  stays at 9:00 wall-clock across daylight-saving changes.
- Padding is added before and after each block/event, never around tasks.
- Calendar padding overrides replace the global padding for that calendar's events.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterable

from dateutil.rrule import rrulestr

from .scheduler import Interval

UTC = timezone.utc
MAX_OCCURRENCES_PER_BLOCK = 4000

_RRULE_SHAPE = re.compile(r"^[A-Za-z0-9=;,:+\-]+$")
_FREQ = re.compile(r"(?:^|;)FREQ=(DAILY|WEEKLY)(?:;|$)", re.IGNORECASE)
_UNTIL = re.compile(r"UNTIL=([0-9]{8}(?:T[0-9]{6}Z?)?)", re.IGNORECASE)


@dataclass(frozen=True)
class BlockIn:
    start: datetime
    end: datetime
    rrule: str | None = None


@dataclass(frozen=True)
class EventIn:
    calendar_id: str
    start: datetime
    end: datetime
    dismissed: bool = False


def _normalize_rule_text(text: str, tz: tzinfo) -> str:
    """Strip an optional 'RRULE:' prefix and make UNTIL compatible with a naive local dtstart."""
    text = text.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]

    def fix(match: re.Match) -> str:
        value = match.group(1).upper()
        if len(value) == 8:  # date only: include that whole day
            return f"UNTIL={value}T235959"
        if value.endswith("Z"):
            utc_dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return "UNTIL=" + utc_dt.astimezone(tz).strftime("%Y%m%dT%H%M%S")
        return f"UNTIL={value}"

    return _UNTIL.sub(fix, text)


def validate_rrule(text: str) -> None:
    """Raise ValueError unless `text` is a single daily or weekly RRULE we can expand."""
    if len(text) > 500 or not _RRULE_SHAPE.match(text.strip()):
        raise ValueError("rrule must be a single RRULE string such as FREQ=WEEKLY;BYDAY=MO,WE")
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[6:]
    if not _FREQ.search(body):
        raise ValueError("rrule FREQ must be DAILY or WEEKLY")
    try:
        rrulestr(_normalize_rule_text(text, UTC), dtstart=datetime(2000, 1, 3))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"invalid rrule: {exc}") from exc


def expand_block(block: BlockIn, tz: tzinfo, lo: datetime, hi: datetime, pad: timedelta) -> list[tuple[datetime, datetime]]:
    """Occurrences of `block` that touch [lo, hi] once padded, as UTC intervals (unpadded)."""
    if not block.rrule:
        if block.end + pad > lo and block.start - pad < hi:
            return [(block.start, block.end)]
        return []

    duration = block.end - block.start
    dtstart = block.start.astimezone(tz).replace(tzinfo=None)
    rule = rrulestr(_normalize_rule_text(block.rrule, tz), dtstart=dtstart)
    window_lo = (lo - duration - pad).astimezone(tz).replace(tzinfo=None) - timedelta(days=1)
    window_hi = (hi + pad).astimezone(tz).replace(tzinfo=None) + timedelta(days=1)
    out: list[tuple[datetime, datetime]] = []
    for occ in rule.xafter(window_lo, count=MAX_OCCURRENCES_PER_BLOCK, inc=True):
        if occ > window_hi:
            break
        start = occ.replace(tzinfo=tz).astimezone(UTC)
        out.append((start, start + duration))
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
) -> list[Interval]:
    """Padded busy intervals touching [lo, hi]. Dismissed events are skipped."""
    out: list[Interval] = []
    gpad = timedelta(minutes=global_padding_min)
    for block in blocks:
        for s, e in expand_block(block, tz, lo, hi, gpad):
            out.append(Interval(s - gpad, e + gpad))
    for ev in events:
        if ev.dismissed:
            continue
        override = calendar_padding.get(ev.calendar_id)
        pad = timedelta(minutes=global_padding_min if override is None else override)
        if ev.end + pad > lo and ev.start - pad < hi:
            out.append(Interval(ev.start - pad, ev.end + pad))
    return out
