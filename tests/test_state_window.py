"""The Google event window used by GET /state (pure date arithmetic, no database or Google)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.routes.data import google_window

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def test_default_window_is_30_local_days_either_side_of_today():
    now = datetime(2026, 10, 8, 18, 30, tzinfo=UTC)  # 14:30 in New York
    start, end = google_window(now, NY, 0)
    assert start == datetime(2026, 9, 8, 0, 0, tzinfo=NY)
    assert end == datetime(2026, 11, 7, 0, 0, tzinfo=NY)


def test_offset_shifts_the_center_in_whole_days():
    now = datetime(2026, 10, 8, 18, 30, tzinfo=UTC)
    assert google_window(now, NY, 30) == (datetime(2026, 10, 8, tzinfo=NY), datetime(2026, 12, 7, tzinfo=NY))
    assert google_window(now, NY, -30) == (datetime(2026, 8, 9, tzinfo=NY), datetime(2026, 10, 8, tzinfo=NY))


def test_today_is_the_date_in_the_users_zone_not_in_utc():
    now = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)  # still Oct 8 evening in New York
    start, _ = google_window(now, NY, 0)
    assert start == datetime(2026, 9, 8, tzinfo=NY)


def test_edges_stay_on_local_midnight_across_a_daylight_saving_change():
    now = datetime(2026, 10, 8, 18, 30, tzinfo=UTC)  # the window spans the Nov 1 clock change
    start, end = google_window(now, NY, 0)
    assert (start.hour, start.minute, end.hour, end.minute) == (0, 0, 0, 0)
    assert end.utcoffset() != start.utcoffset()  # EDT at the start, EST at the end
    assert (end.date() - start.date()).days == 60
