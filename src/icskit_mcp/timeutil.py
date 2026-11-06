"""Parsing and formatting of the times users and agents type, plus the
interval arithmetic behind free-time and conflict detection."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from .ical import CalendarError
from .model import Occurrence, place
from .tz import UTC, require_zone

MAX_WINDOW_DAYS = 3660


def parse_when(text: str, what: str = "time") -> date | datetime:
    """'2026-03-02' -> date; '2026-03-02T09:30' or '2026-03-02 09:30' -> naive
    datetime; with a trailing Z or +hh:mm offset -> aware datetime."""
    t = text.strip()
    if not t:
        raise CalendarError(f"{what} is empty; use e.g. 2026-03-02 or 2026-03-02T09:30")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            return date.fromisoformat(t)
        t2 = t.replace(" ", "T", 1)
        if t2.endswith(("Z", "z")):
            t2 = t2[:-1] + "+00:00"
        return datetime.fromisoformat(t2)
    except ValueError:
        raise CalendarError(
            f"cannot read {what} {text!r}; use 2026-03-02 (a date) or 2026-03-02T09:30 "
            f"(date and time, optionally with Z or an offset like +03:00)"
        ) from None


def to_zone(value: date | datetime, tz: tzinfo, end_of_day: bool = False) -> datetime:
    """A date becomes local midnight (or the next midnight with end_of_day);
    a naive datetime is read in tz; an aware one is converted to tz."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return place(value, tz)
        return value.astimezone(UTC).astimezone(tz)
    d = value + timedelta(days=1) if end_of_day else value
    return place(datetime.combine(d, time.min), tz)


def make_window(start: str, end: str, tz_name: str) -> tuple[datetime, datetime, tzinfo]:
    """Parse a [start, end) window. A date-only `end` includes that whole day."""
    tz = require_zone(tz_name)
    s_raw = parse_when(start, "start")
    e_raw = parse_when(end, "end")
    ws = to_zone(s_raw, tz)
    we = to_zone(e_raw, tz, end_of_day=not isinstance(e_raw, datetime))
    if we <= ws:
        raise CalendarError(f"end ({end}) must be after start ({start})")
    if we - ws > timedelta(days=MAX_WINDOW_DAYS):
        raise CalendarError(f"window is longer than {MAX_WINDOW_DAYS} days; ask for a shorter range")
    return ws, we, tz


def zone_name(tz: tzinfo) -> str:
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    return "UTC" if tz is UTC else str(tz)


def fmt_dt(dt: datetime, tz: tzinfo) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or "")


def fmt_time(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def fmt_offset(dt: datetime, tz: tzinfo) -> str:
    off = dt.astimezone(tz).strftime("%z")
    return f"{off[:3]}:{off[3:]}" if off else "+00:00"


def fmt_duration(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h{m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def parse_working_hours(text: str) -> list[tuple[time, time | None]]:
    """'09:00-17:00' or '09:00-12:30,13:30-17:00'. '24:00' means end of day
    (returned as None)."""
    out: list[tuple[time, time | None]] = []
    for chunk in text.split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", chunk)
        if not m:
            raise CalendarError(
                f"working_hours {text!r} is not like '09:00-17:00' or '09:00-12:00,13:00-17:00'"
            )
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 > 23 or m1 > 59 or m2 > 59 or h2 > 24 or (h2 == 24 and m2 != 0):
            raise CalendarError(f"working_hours {text!r} has an impossible time")
        t1 = time(h1, m1)
        t2: time | None = None if (h2, m2) == (24, 0) else time(h2, m2)
        if t2 is not None and t2 <= t1:
            raise CalendarError(f"working_hours {text!r}: {chunk.strip()} ends before it starts")
        out.append((t1, t2))
    return out


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_days(text: str) -> set[int]:
    """'Mon-Fri', 'Mon,Wed,Fri', 'Sat-Sun', 'all' -> weekday numbers (Mon=0)."""
    t = text.strip().lower()
    if t in ("all", "daily", "everyday", "*"):
        return set(range(7))
    out: set[int] = set()
    for chunk in t.split(","):
        c = chunk.strip()
        if "-" in c:
            a, b = (x.strip()[:3] for x in c.split("-", 1))
            if a not in _DAYS or b not in _DAYS:
                raise CalendarError(f"days {text!r}: use names like Mon-Fri or Mon,Wed")
            i = _DAYS.index(a)
            while True:
                out.add(i)
                if i == _DAYS.index(b):
                    break
                i = (i + 1) % 7
        else:
            if c[:3] not in _DAYS:
                raise CalendarError(f"days {text!r}: use names like Mon-Fri or Mon,Wed")
            out.add(_DAYS.index(c[:3]))
    return out


# --- interval arithmetic ----------------------------------------------------------

Interval = tuple[datetime, datetime]


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Union of half-open intervals; touching intervals are joined."""
    items = sorted(intervals)
    out: list[Interval] = []
    for s, e in items:
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def subtract(free: list[Interval], busy: list[Interval]) -> list[Interval]:
    """free minus busy; both must be merged and sorted."""
    out: list[Interval] = []
    j = 0
    for s, e in free:
        cur = s
        while j < len(busy) and busy[j][1] <= cur:
            j += 1
        k = j
        while k < len(busy) and busy[k][0] < e:
            bs, be = busy[k]
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
            k += 1
        if cur < e:
            out.append((cur, e))
    return out


def working_intervals(
    ws: datetime, we: datetime, tz: tzinfo, hours: list[tuple[time, time | None]], days: set[int]
) -> list[Interval]:
    """Working time inside [ws, we), as UTC-comparable aware intervals, built
    from local wall clocks so DST days keep their wall hours."""
    out: list[Interval] = []
    ws, we = ws.astimezone(UTC), we.astimezone(UTC)
    d = ws.astimezone(tz).date() - timedelta(days=1)
    last = we.astimezone(tz).date() + timedelta(days=1)
    while d <= last:
        if d.weekday() in days:
            for t1, t2 in hours:
                s = place(datetime.combine(d, t1), tz)
                e = (
                    place(datetime.combine(d + timedelta(days=1), time.min), tz)
                    if t2 is None
                    else place(datetime.combine(d, t2), tz)
                )
                s, e = max(s.astimezone(UTC), ws), min(e.astimezone(UTC), we)
                if e > s:
                    out.append((s, e))
        d += timedelta(days=1)
    return merge(out)


def busy_intervals(
    occs: Iterable[Occurrence],
    qtz: tzinfo,
    buffer: timedelta,
    include_all_day: bool,
    include_transparent: bool,
) -> list[Interval]:
    out: list[Interval] = []
    for o in occs:
        if o.event.cancelled:
            continue
        if o.all_day and not include_all_day:
            continue
        if o.event.transparent and not include_transparent:
            continue
        s, e = o.span(qtz)
        if e <= s:  # an instant blocks nothing
            continue
        out.append((s - buffer, e + buffer))
    return merge(out)


@dataclass(frozen=True)
class Conflict:
    a: Occurrence
    b: Occurrence
    start: datetime
    end: datetime


def find_overlaps(occs: list[Occurrence], qtz: tzinfo) -> list[Conflict]:
    """Every pair of occurrences whose time ranges overlap (touching is fine)."""
    items = []
    for o in occs:
        s, e = o.span(qtz)
        items.append((s.astimezone(UTC), e.astimezone(UTC), o))
    items.sort(key=lambda t: (t[0], t[1]))
    out: list[Conflict] = []
    active: list[tuple[datetime, datetime, Occurrence]] = []
    for s, e, o in items:
        active = [a for a in active if a[1] > s]
        for s2, e2, o2 in active:
            lo, hi = max(s, s2), min(e, e2)
            if lo < hi or (e == s and s2 <= s < e2) or (e2 == s2 and s <= s2 < e):
                out.append(Conflict(o2, o, lo, hi))
        if e > s:
            active.append((s, e, o))
    return out
