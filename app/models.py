"""Request/response shapes. The same JSON shapes are used by every endpoint (see SPEC.md)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .busy import validate_rrule

MAX_TASKS = 200
MAX_BLOCKS = 500
MAX_LOCKED_CHUNKS = 2000
MAX_CALENDARS = 100
MAX_SYNC_ITEMS = 5000


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Padding = Annotated[int, Field(ge=0, le=240)]


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError("unknown timezone; use an IANA name such as America/New_York") from exc
    return value


class Shape(BaseModel):
    # Unknown fields are ignored so a newer frontend does not break an older backend.
    model_config = ConfigDict(extra="ignore")


# ----------------------------------------------------------------------------- settings

class Settings(Shape):
    padding_min: Padding = 15
    work_start: HHMM = "08:00"
    work_end: HHMM = "22:00"
    spread_mode: Literal["even", "front_load"] = "even"
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_timezone(v)

    @model_validator(mode="after")
    def _window(self) -> "Settings":
        if self.work_end <= self.work_start:
            raise ValueError("work_end must be later than work_start (overnight windows are not supported)")
        return self


class SettingsPatch(Shape):
    padding_min: Padding | None = None
    work_start: HHMM | None = None
    work_end: HHMM | None = None
    spread_mode: Literal["even", "front_load"] | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else _check_timezone(v)


# --------------------------------------------------------------------------- core items

class Task(Shape):
    id: UUID
    title: Title
    due_at: UtcDatetime
    duration_min: int = Field(ge=1, le=10080)
    splittable: bool = False
    min_chunk_min: int | None = Field(default=None, ge=1, le=10080)

    @model_validator(mode="after")
    def _clamp_min_chunk(self) -> "Task":
        if self.min_chunk_min is not None and self.min_chunk_min > self.duration_min:
            self.min_chunk_min = self.duration_min
        return self


class Block(Shape):
    id: UUID
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    start: UtcDatetime
    end: UtcDatetime
    rrule: Annotated[str, StringConstraints(max_length=500)] | None = None

    @field_validator("rrule")
    @classmethod
    def _rrule(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        validate_rrule(v)
        return v.strip()

    @model_validator(mode="after")
    def _order(self) -> "Block":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if (self.end - self.start).days > 366:
            raise ValueError("a block cannot be longer than 366 days")
        return self


class Chunk(Shape):
    id: UUID
    task_id: UUID
    start: UtcDatetime
    end: UtcDatetime
    locked: bool = False

    @model_validator(mode="after")
    def _order(self) -> "Chunk":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Calendar(Shape):
    id: str
    name: str
    selected: bool = False
    padding_override_min: Padding | None = None


class CalendarPatch(Shape):
    """Only fields that are present are applied. `padding_override_min: null` clears the override."""

    id: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    selected: bool | None = None
    padding_override_min: Padding | None = None


class Event(Shape):
    id: str
    recurring_event_id: str | None = None
    calendar_id: str
    title: str = ""
    start: UtcDatetime
    end: UtcDatetime
    dismissed: bool = False


class Dismissal(Shape):
    id: UUID
    calendar_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    google_event_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    scope: Literal["occurrence", "series"] = "occurrence"


# ----------------------------------------------------------------------------- /sync

T = TypeVar("T")


class Changes(Shape, Generic[T]):
    upsert: list[T] = Field(default_factory=list, max_length=MAX_SYNC_ITEMS)
    delete: list[UUID] = Field(default_factory=list, max_length=MAX_SYNC_ITEMS)


class SyncRequest(Shape):
    settings: SettingsPatch | None = None
    calendars: list[CalendarPatch] = Field(default_factory=list, max_length=MAX_CALENDARS)
    tasks: Changes[Task] = Field(default_factory=Changes)
    blocks: Changes[Block] = Field(default_factory=Changes)
    chunks: Changes[Chunk] = Field(default_factory=Changes)
    dismissed_events: Changes[Dismissal] = Field(default_factory=Changes)


class SyncResponse(Shape):
    ok: bool = True
    server_time: UtcDatetime


# --------------------------------------------------------------------------- /schedule

class ScheduleRequest(Shape):
    tasks: list[Task] = Field(default_factory=list, max_length=MAX_TASKS)
    blocks: list[Block] = Field(default_factory=list, max_length=MAX_BLOCKS)
    settings: Settings = Field(default_factory=Settings)
    calendars: list[CalendarPatch] | None = Field(default=None, max_length=MAX_CALENDARS)
    locked_chunks: list[Chunk] = Field(default_factory=list, max_length=MAX_LOCKED_CHUNKS)
    now: UtcDatetime | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> "ScheduleRequest":
        for name in ("tasks", "blocks", "locked_chunks"):
            ids = [item.id for item in getattr(self, name)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate ids in {name}")
        return self


class UnschedulableItem(Shape):
    task_id: UUID
    missing_min: int


class WarningItem(Shape):
    code: str
    message: str
    task_id: UUID | None = None
    chunk_id: UUID | None = None


class ScheduleResponse(Shape):
    chunks: list[Chunk]
    unschedulable: list[UnschedulableItem]
    google_events: list[Event]
    warnings: list[WarningItem]
    google_error: str | None = None


# ------------------------------------------------------------------------------ /state

class UserInfo(Shape):
    email: str
    timezone: str


class StateResponse(Shape):
    user: UserInfo
    settings: Settings
    calendars: list[Calendar]
    tasks: list[Task]
    blocks: list[Block]
    chunks: list[Chunk]
    dismissed_events: list[Dismissal]
    google_events: list[Event]
    server_time: UtcDatetime
    google_error: str | None = None
