"""Time zone handling: IANA zones via zoneinfo, and tzinfo objects built from
a calendar's own VTIMEZONE components when the TZID is not an IANA name
(Outlook's "W. Europe Standard Time", for example).

Wall-clock to instant policy (RFC 5545 section 3.3.5): a wall time that
occurs twice (clocks go back) means the FIRST occurrence; a wall time that
does not exist (clocks go forward) is read with the offset in effect BEFORE
the transition. zoneinfo's fold=0 behaves the same way, and VTimezone below
implements the same policy, so both kinds of zone agree.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ical import CalendarError, Component, parse_datetime, parse_utc_offset
from .rrule import iterate, parse_rule


class TimezoneError(CalendarError):
    """A time zone name could not be resolved."""


UTC = timezone.utc

_ZONE_CACHE: dict[str, ZoneInfo] = {}


def iana_zone(name: str) -> ZoneInfo | None:
    cached = _ZONE_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        z = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    _ZONE_CACHE[name] = z
    return z


def require_zone(name: str) -> tzinfo:
    """Resolve a user-supplied zone name or raise an instructive error."""
    n = name.strip()
    if not n:
        raise TimezoneError("time zone is empty; pass an IANA name such as 'Europe/Istanbul' or 'UTC'")
    if n.upper() in ("Z", "UTC"):
        return UTC
    z = iana_zone(n)
    if z is None:
        raise TimezoneError(
            f"unknown time zone {name!r}. Use an IANA name such as 'Europe/Istanbul', "
            f"'America/New_York' or 'UTC' (not an abbreviation like EST)."
        )
    return z


class VTimezone(tzinfo):
    """tzinfo built from a VTIMEZONE component."""

    def __init__(self, tzid: str, comp: Component) -> None:
        self._tzid = tzid
        self._subs: list[tuple[datetime, timedelta, timedelta, str, Component]] = []
        for sub in comp.children:
            if sub.name not in ("STANDARD", "DAYLIGHT"):
                continue
            ds, of, ot = sub.get("DTSTART"), sub.get("TZOFFSETFROM"), sub.get("TZOFFSETTO")
            if ds is None or of is None or ot is None:
                raise TimezoneError(
                    f"VTIMEZONE {tzid!r} has a {sub.name} block missing DTSTART/TZOFFSETFROM/TZOFFSETTO"
                )
            start, _ = parse_datetime(ds.value)
            name = sub.get("TZNAME")
            self._subs.append(
                (start, parse_utc_offset(of.value), parse_utc_offset(ot.value),
                 name.value if name else "", sub)
            )
        if not self._subs:
            raise TimezoneError(f"VTIMEZONE {tzid!r} has no STANDARD or DAYLIGHT block")
        self._built_to = 0
        self._offsets: set[timedelta] = set()
        # (utc instant as naive datetime, offset after, name), sorted
        self._trans: list[tuple[datetime, timedelta, str]] = []
        self._instants: list[datetime] = []
        self._first_offset = min(self._subs, key=lambda s: s[0])[1]
        self._first_name = ""
        self._extend(2100)

    def _extend(self, upto_year: int) -> None:
        trans: list[tuple[datetime, timedelta, str]] = []
        for start, off_from, off_to, name, sub in self._subs:
            onsets: list[datetime] = [start]
            rr = sub.get("RRULE")
            if rr is not None:
                rule = parse_rule(rr.value)
                until: datetime | None = None
                if rule.until is not None and isinstance(rule.until, datetime):
                    # VTIMEZONE UNTIL is UTC; onsets are wall times in the old offset.
                    until = rule.until + off_from if rule.until_utc else rule.until
                hi = datetime(upto_year, 12, 31)
                # Outlook starts these rules in year 1601; nothing earlier than 1900 matters.
                lo = max(start, datetime(1900, 1, 1))
                onsets = [
                    o
                    for o in iterate(rule, start, lo=lo, hi=hi, until=until)
                    if isinstance(o, datetime)
                ]
            for rd in sub.get_all("RDATE"):
                for part in rd.value.split(","):
                    if part.strip():
                        onsets.append(parse_datetime(part)[0])
            for onset in onsets:
                trans.append((onset - off_from, off_to, name))
        trans.sort(key=lambda t: t[0])
        self._trans = trans
        self._instants = [t[0] for t in trans]
        self._built_to = upto_year
        self._offsets = {t[1] for t in trans} | {self._first_offset}

    def _at_utc(self, utc_naive: datetime) -> tuple[timedelta, str]:
        i = bisect.bisect_right(self._instants, utc_naive) - 1
        if i < 0:
            return self._first_offset, self._first_name
        return self._trans[i][1], self._trans[i][2]

    def _offset_for_wall(self, wall: datetime, fold: int) -> tuple[timedelta, str]:
        if wall.year > self._built_to:
            self._extend(wall.year + 50)
        offsets = self._offsets
        valid: list[tuple[timedelta, str]] = []
        for o in offsets:
            at, nm = self._at_utc(wall - o)
            if at == o:
                valid.append((o, nm))
        if len(valid) == 1:
            return valid[0]
        if len(valid) >= 2:  # clocks went back: fold=0 is the first occurrence
            valid.sort(key=lambda v: v[0], reverse=(fold == 0))
            return valid[0]
        # Gap (clocks went forward): fold=0 uses the offset in effect just
        # before the transition (the lower one, RFC 5545); fold=1 the one after
        # it (PEP 495), which is what zoneinfo does too.
        results = [self._at_utc(wall - o) for o in offsets]
        return max(results, key=lambda r: r[0]) if fold == 1 else min(results, key=lambda r: r[0])

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return self._offset_for_wall(dt.replace(tzinfo=None), dt.fold)[0]

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str | None:
        if dt is None:
            return self._tzid
        nm = self._offset_for_wall(dt.replace(tzinfo=None), dt.fold)[1]
        return nm or self._tzid

    def fromutc(self, dt: datetime) -> datetime:
        naive = dt.replace(tzinfo=None)
        off, _ = self._at_utc(naive)
        wall = (naive + off).replace(tzinfo=self)
        # Second pass through a repeated hour (clocks went back): mark fold=1
        # so utcoffset() answers with the later offset, as zoneinfo does.
        if self._offset_for_wall(wall.replace(tzinfo=None), 0)[0] != off:
            wall = wall.replace(fold=1)
        return wall

    def __repr__(self) -> str:
        return f"VTimezone({self._tzid!r})"


class TzResolver:
    """Resolves TZID values for one calendar file."""

    def __init__(self, vtimezones: list[Component]) -> None:
        self._vtz: dict[str, Component] = {}
        for c in vtimezones:
            p = c.get("TZID")
            if p is not None:
                self._vtz[p.value.strip()] = c
        self._cache: dict[str, tzinfo] = {}

    def get(self, tzid: str) -> tzinfo:
        hit = self._cache.get(tzid)
        if hit is not None:
            return hit
        zone = self._resolve(tzid)
        self._cache[tzid] = zone
        return zone

    def _resolve(self, tzid: str) -> tzinfo:
        name = tzid.strip().strip('"')
        # Prefer a real IANA zone: it carries the full history of rule changes.
        z = iana_zone(name)
        if z is not None:
            return z
        if name.upper() in ("UTC", "Z", "GMT"):
            return UTC
        # Some producers prefix a path: /mozilla.org/20050126_1/Europe/Berlin
        if "/" in name:
            parts = [p for p in name.split("/") if p]
            for i in range(len(parts)):
                z = iana_zone("/".join(parts[i:]))
                if z is not None:
                    return z
        comp = self._vtz.get(name)
        if comp is not None:
            return VTimezone(name, comp)
        raise TimezoneError(
            f"time zone {tzid!r} is not an IANA name and the file has no VTIMEZONE "
            f"for it; cannot place the event in time"
        )
