"""Events, recurrence expansion and occurrence spans.

Everything that turns "a VEVENT" into "the intervals it occupies" lives here.

Key rules (see tz.py for the DST policy):
  * recurrences are expanded in WALL-CLOCK time of the event's own zone, then
    each occurrence is placed on the time line - so a 09:00 Berlin meeting is
    09:00 Berlin on both sides of a DST change;
  * a DTEND-defined length is exact elapsed time, a DURATION in days/weeks is
    nominal (calendar) days;
  * all-day events are dates, not instants; they occupy whole local days in
    whatever zone the caller asks about;
  * floating times (no zone, no Z) are read in the zone the caller asks about.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

from .ical import (
    Component,
    Duration,
    ICalError,
    Prop,
    is_date_value,
    parse_date,
    parse_datetime,
    parse_duration,
    split_text_list,
    unescape_text,
)
from .rrule import Rule, Wall, iterate, parse_rule
from .tz import UTC, TzResolver

# --- time values ------------------------------------------------------------


@dataclass(frozen=True)
class TimeVal:
    """A parsed DATE / DATE-TIME property value.

    kind: "date" (all-day), "floating", "utc" or "zoned".
    wall: date for "date"; naive datetime otherwise.
    """

    kind: str
    wall: date | datetime
    tz: tzinfo | None = None
    tzid: str | None = None

    @property
    def is_date(self) -> bool:
        return self.kind == "date"

    def aware(self, floating: tzinfo) -> datetime:
        if not isinstance(self.wall, datetime):
            raise ICalError("an all-day value has no time of day")
        return place(self.wall, self.tz or floating)

    def instant(self, floating: tzinfo) -> datetime:
        """The same moment as an aware UTC datetime. Use this, not aware(), for
        comparing or subtracting: Python compares two datetimes that share one
        tzinfo by wall clock, which is wrong across a DST change."""
        return self.aware(floating).astimezone(UTC)

    def key(self) -> tuple[str, date | datetime]:
        return value_key(self.wall, self.tz)


def place(wall: datetime, tz: tzinfo) -> datetime:
    """Wall clock -> aware datetime, RFC 5545 gap/overlap policy, normalised."""
    return wall.replace(tzinfo=tz).astimezone(UTC).astimezone(tz)


def value_key(wall: date | datetime, tz: tzinfo | None) -> tuple[str, date | datetime]:
    if not isinstance(wall, datetime):
        return ("d", wall)
    if tz is None:
        return ("w", wall)
    return ("u", wall.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None))


def parse_timeval(prop: Prop, resolver: TzResolver) -> TimeVal:
    v = prop.value.strip()
    if is_date_value(v) and (prop.param("VALUE") in (None, "DATE")):
        return TimeVal("date", parse_date(v))
    wall, is_utc = parse_datetime(v)
    if is_utc:
        return TimeVal("utc", wall, UTC, "UTC")
    tzid = prop.param("TZID")
    if tzid:
        return TimeVal("zoned", wall, resolver.get(tzid), tzid)
    return TimeVal("floating", wall)


# --- events -------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    address: str
    name: str | None = None
    partstat: str | None = None
    role: str | None = None

    def label(self) -> str:
        base = f"{self.name} <{self.address}>" if self.name else self.address
        return f"{base} ({self.partstat.lower()})" if self.partstat else base


def _person(p: Prop) -> Person:
    addr = p.value.strip()
    if addr.lower().startswith("mailto:"):
        addr = addr[7:]
    cn = p.param("CN")
    if cn is not None:  # some exporters backslash-escape commas inside CN
        cn = cn.replace("\\,", ",")
    return Person(addr, cn, p.param("PARTSTAT"), p.param("ROLE"))


@dataclass
class Event:
    uid: str
    calendar: str  # relative path of the file
    comp: Component
    summary: str
    description: str
    location: str
    status: str
    transp: str
    start: TimeVal
    end: TimeVal | None
    duration: Duration | None
    rule: Rule | None
    exdates: list[TimeVal]
    rdates: list[TimeVal]
    recurrence_id: TimeVal | None
    attendees: list[Person]
    organizer: Person | None
    categories: list[str]
    sequence: int
    extra_warnings: list[str] = field(default_factory=list)

    @property
    def all_day(self) -> bool:
        return self.start.is_date

    @property
    def recurring(self) -> bool:
        return self.rule is not None or bool(self.rdates)

    @property
    def cancelled(self) -> bool:
        return self.status == "CANCELLED"

    @property
    def transparent(self) -> bool:
        return self.transp == "TRANSPARENT"

    def title(self) -> str:
        return self.summary or "(no title)"


def _text(comp: Component, name: str) -> str:
    p = comp.get(name)
    return unescape_text(p.value) if p else ""


def _upper(comp: Component, name: str, default: str) -> str:
    p = comp.get(name)
    return p.value.strip().upper() if p else default


def _timevals(comp: Component, name: str, resolver: TzResolver) -> list[TimeVal]:
    out: list[TimeVal] = []
    for p in comp.get_all(name):
        for piece in p.value.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if "/" in piece:  # RDATE;VALUE=PERIOD start/end-or-duration: only the start matters
                piece = piece.split("/", 1)[0]
            out.append(parse_timeval(Prop(p.name, p.params, piece, p.first, p.last), resolver))
    return out


def build_event(comp: Component, calendar: str, resolver: TzResolver) -> Event:
    """Interpret a VEVENT. Raises CalendarError describing what is wrong."""
    ds = comp.get("DTSTART")
    if ds is None:
        raise ICalError("VEVENT has no DTSTART")
    start = parse_timeval(ds, resolver)
    end: TimeVal | None = None
    duration: Duration | None = None
    de, du = comp.get("DTEND"), comp.get("DURATION")
    if de is not None:
        end = parse_timeval(de, resolver)
        if end.is_date != start.is_date:
            raise ICalError("DTSTART and DTEND must both be dates or both be date-times")
    elif du is not None:
        duration = parse_duration(du.value)
        if duration.sign < 0:
            raise ICalError("DURATION is negative")
    uid_p = comp.get("UID")
    uid = uid_p.value.strip() if uid_p and uid_p.value.strip() else f"no-uid:{calendar}:L{comp.first + 1}"
    rr = comp.get("RRULE")
    rule = parse_rule(rr.value) if rr else None
    rid_p = comp.get("RECURRENCE-ID")
    seq_p = comp.get("SEQUENCE")
    try:
        seq = int(seq_p.value) if seq_p else 0
    except ValueError:
        seq = 0
    cats: list[str] = []
    for c in comp.get_all("CATEGORIES"):
        cats.extend(split_text_list(c.value))
    org = comp.get("ORGANIZER")
    ev = Event(
        uid=uid,
        calendar=calendar,
        comp=comp,
        summary=_text(comp, "SUMMARY"),
        description=_text(comp, "DESCRIPTION"),
        location=_text(comp, "LOCATION"),
        status=_upper(comp, "STATUS", ""),
        transp=_upper(comp, "TRANSP", "OPAQUE"),
        start=start,
        end=end,
        duration=duration,
        rule=rule,
        exdates=_timevals(comp, "EXDATE", resolver),
        rdates=_timevals(comp, "RDATE", resolver),
        recurrence_id=parse_timeval(rid_p, resolver) if rid_p else None,
        attendees=[_person(p) for p in comp.get_all("ATTENDEE")],
        organizer=_person(org) if org else None,
        categories=cats,
        sequence=seq,
    )
    # Validate the length now so a bad event is reported once, at load time.
    if end is not None:
        before = (
            end.wall < start.wall
            if start.is_date
            else end.instant(UTC) < start.instant(UTC)
        )
        if before:
            raise ICalError("DTEND is before DTSTART")
    return ev


@dataclass
class Series:
    """All VEVENTs sharing one UID in one file: the master and its overrides."""

    uid: str
    calendar: str
    master: Event | None
    overrides: list[Event]

    @property
    def events(self) -> list[Event]:
        return ([self.master] if self.master else []) + self.overrides


# --- occurrences ----------------------------------------------------------------


@dataclass(frozen=True)
class Occurrence:
    event: Event
    all_day: bool
    start: datetime | date  # aware datetime, or date for all-day
    end: datetime | date  # exclusive for all-day
    recurring: bool  # belongs to a recurring series (master instance or override)
    override: bool  # an explicit RECURRENCE-ID exception

    def span(self, qtz: tzinfo) -> tuple[datetime, datetime]:
        """[start, end) on the time line as aware UTC datetimes (safe to compare
        and subtract); all-day dates become whole local days in qtz."""
        if isinstance(self.start, datetime) and isinstance(self.end, datetime):
            return self.start.astimezone(UTC), self.end.astimezone(UTC)
        assert isinstance(self.start, date) and isinstance(self.end, date)
        return (
            place(datetime.combine(self.start, time.min), qtz).astimezone(UTC),
            place(datetime.combine(self.end, time.min), qtz).astimezone(UTC),
        )


def _length(ev: Event, floating: tzinfo) -> tuple[int, timedelta]:
    """(nominal days, exact timedelta) such that end = place(wall + days) + exact.
    For all-day events only the day count matters."""
    if ev.start.is_date:
        assert isinstance(ev.start.wall, date)
        if ev.end is not None:
            assert isinstance(ev.end.wall, date)
            return max((ev.end.wall - ev.start.wall).days, 1), timedelta(0)
        if ev.duration is not None:
            return ev.duration.days + (ev.duration.seconds + 86399) // 86400, timedelta(0)
        return 1, timedelta(0)
    if ev.end is not None:
        return 0, ev.end.instant(floating) - ev.start.instant(floating)
    if ev.duration is not None:
        return ev.duration.days, timedelta(seconds=ev.duration.seconds)
    return 0, timedelta(0)


def event_span(ev: Event, wall: Wall, floating: tzinfo) -> tuple[datetime | date, datetime | date]:
    days, exact = _length(ev, floating)
    if isinstance(wall, datetime):
        zone = ev.start.tz or floating
        s = place(wall, zone)
        if days:
            e = place(wall + timedelta(days=days), zone) + exact
        else:
            e = (s.astimezone(UTC) + exact).astimezone(zone)
        return s, e
    return wall, wall + timedelta(days=days)


def _overlaps(s: datetime, e: datetime, ws: datetime, we: datetime) -> bool:
    if e > s:
        return s < we and e > ws
    return ws <= s < we  # an instant counts if it falls inside the window


def _to_zone_wall(v: TimeVal, zone: tzinfo, floating: tzinfo) -> Wall:
    """Express an RDATE/UNTIL-style value as a wall value in the master's zone."""
    if isinstance(v.wall, datetime):
        if v.tz is None:
            return v.wall
        return v.aware(floating).astimezone(zone).replace(tzinfo=None)
    return v.wall


def _walls(ev: Event, ws: datetime, we: datetime, floating: tzinfo) -> list[Wall]:
    """Wall-clock starts of every instance of `ev` that could touch the window."""
    if not ev.recurring:
        return [ev.start.wall]
    days, exact = _length(ev, floating)
    pad = timedelta(days=days + 2) + max(exact, timedelta(0))
    if ev.start.is_date:
        assert isinstance(ev.start.wall, date)
        lo: Wall = (ws.astimezone(floating).replace(tzinfo=None) - pad).date()
        hi: Wall = (we.astimezone(floating).replace(tzinfo=None) + timedelta(days=1)).date()
        dtstart: Wall = ev.start.wall
        zone: tzinfo = floating
    else:
        zone = ev.start.tz or floating
        lo = ws.astimezone(zone).replace(tzinfo=None) - pad
        hi = we.astimezone(zone).replace(tzinfo=None) + timedelta(days=1)
        dtstart = ev.start.wall
    walls: dict[Wall, None] = {}
    if ev.rule is not None:
        until: Wall | None = None
        if ev.rule.until is not None:
            u = ev.rule.until
            if isinstance(u, datetime) and ev.rule.until_utc:
                until = place(u.replace(tzinfo=None), UTC).astimezone(zone).replace(tzinfo=None)
                if ev.start.is_date:
                    until = until.date()
            else:
                until = u
        for w in iterate(ev.rule, dtstart, lo, hi, until):
            walls[w] = None
    else:
        walls[dtstart] = None
    for rd in ev.rdates:
        w = _to_zone_wall(rd, zone, floating)
        if isinstance(dtstart, datetime) and not isinstance(w, datetime):
            w = datetime.combine(w, dtstart.time())
        if isinstance(dtstart, date) and not isinstance(dtstart, datetime) and isinstance(w, datetime):
            w = w.date()
        walls[w] = None
    return list(walls)


def _keys_of(vals: Iterable[TimeVal]) -> set[tuple[str, date | datetime]]:
    return {v.key() for v in vals}


def _is_hidden(wall: Wall, zone: tzinfo | None, keys: set[tuple[str, date | datetime]]) -> bool:
    if not keys:
        return False
    if value_key(wall, zone) in keys:
        return True
    if isinstance(wall, datetime):
        return ("d", wall.date()) in keys  # a DATE-valued EXDATE / RECURRENCE-ID
    return False


def expand_series(
    series: Series,
    ws: datetime,
    we: datetime,
    floating: tzinfo,
    include_cancelled: bool = False,
) -> list[Occurrence]:
    """All occurrences of a series overlapping [ws, we) (aware datetimes)."""
    out: list[Occurrence] = []
    ws, we = ws.astimezone(UTC), we.astimezone(UTC)
    master = series.master
    override_keys: set[tuple[str, date | datetime]] = set()
    for ov in series.overrides:
        if ov.recurrence_id is not None:
            override_keys.add(ov.recurrence_id.key())

    if master is not None and (include_cancelled or not master.cancelled):
        zone = master.start.tz
        hidden = _keys_of(master.exdates) | override_keys
        for wall in _walls(master, ws, we, floating):
            if _is_hidden(wall, zone, hidden):
                continue
            s, e = event_span(master, wall, floating)
            occ = Occurrence(master, master.all_day, s, e, master.recurring, False)
            if _overlaps(*occ.span(floating), ws, we):
                out.append(occ)

    for ov in series.overrides:
        if ov.cancelled and not include_cancelled:
            continue
        s, e = event_span(ov, ov.start.wall, floating)
        occ = Occurrence(ov, ov.all_day, s, e, True, True)
        if _overlaps(*occ.span(floating), ws, we):
            out.append(occ)
    return out


def sort_occurrences(occs: list[Occurrence], qtz: tzinfo) -> list[Occurrence]:
    def key(o: Occurrence) -> tuple[datetime, datetime, int, str]:
        s, e = o.span(qtz)
        # all-day entries first within their day
        return (s.astimezone(UTC), e.astimezone(UTC), 0 if o.all_day else 1, o.event.summary)

    return sorted(occs, key=key)
