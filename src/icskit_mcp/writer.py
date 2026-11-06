"""Safe calendar editing by line surgery.

A file is edited by replacing or inserting whole physical lines. Every line
that is not part of the event being changed is carried over byte for byte:
other events, VTIMEZONE blocks, unknown X- properties, folding, line endings,
even a byte-order mark. The result is parsed again before it may be written.

Nothing here touches the disk; commit() does that, and only to a new file
unless the caller asked for in_place.
"""

from __future__ import annotations

import difflib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

from .ical import (
    CalendarError,
    Component,
    Prop,
    escape_text,
    fold_line,
    format_utc,
    parse,
    split_physical,
)
from .library import CalendarFile, Library
from .model import Event, Series, expand_series, place, value_key
from .rrule import parse_rule
from .timeutil import parse_when, to_zone
from .tz import UTC, require_zone

PRODID = "-//icskit-mcp//local calendar tools//EN"
_STATUSES = ("TENTATIVE", "CONFIRMED", "CANCELLED")
_TRANSPS = ("OPAQUE", "TRANSPARENT")


# --- zone specs -------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneSpec:
    kind: str  # "utc" | "zoned" | "floating"
    name: str | None
    tz: tzinfo | None

    @staticmethod
    def of(tz_name: str) -> ZoneSpec:
        z = require_zone(tz_name)
        if z is UTC:
            return ZoneSpec("utc", "UTC", UTC)
        return ZoneSpec("zoned", getattr(z, "key", tz_name.strip()), z)


FLOATING = ZoneSpec("floating", None, None)


def _check_text(value: str, what: str) -> str:
    for ch in value:
        if (ord(ch) < 32 and ch not in "\n\r\t") or ord(ch) == 127:
            raise CalendarError(f"{what} contains a control character (U+{ord(ch):04X}), which is not allowed")
    return value


def _line(name: str, value: str, params: list[tuple[str, str]] | None = None) -> str:
    ps = "".join(f";{k}={v}" for k, v in (params or []))
    return f"{name}{ps}:{value}"


def time_line(name: str, value: date | datetime, spec: ZoneSpec) -> tuple[str, list[str]]:
    """The content line for a DTSTART/DTEND/EXDATE value, plus any notes."""
    notes: list[str] = []
    if isinstance(value, datetime):
        text = value.strftime("%Y%m%dT%H%M%S")
        if spec.kind == "utc":
            return _line(name, text + "Z"), notes
        if spec.kind == "floating":
            return _line(name, text), notes
        assert spec.tz is not None and spec.name is not None
        if place(value, spec.tz).replace(tzinfo=None) != value:
            raise CalendarError(
                f"{value:%Y-%m-%d %H:%M} does not exist in {spec.name} (clocks go forward "
                f"over it); pick a time outside the daylight-saving gap"
            )
        a = value.replace(tzinfo=spec.tz, fold=0).utcoffset()
        b = value.replace(tzinfo=spec.tz, fold=1).utcoffset()
        if a != b:
            notes.append(
                f"{value:%Y-%m-%d %H:%M} happens twice in {spec.name} (clocks go back); "
                f"the first occurrence is meant"
            )
        return _line(name, text, [("TZID", spec.name)]), notes
    return _line(name, value.strftime("%Y%m%d"), [("VALUE", "DATE")]), notes


def _input_value(text: str, spec: ZoneSpec, what: str) -> date | datetime:
    v = parse_when(text, what)
    if isinstance(v, datetime) and v.tzinfo is not None:
        if spec.kind == "floating":
            raise CalendarError(f"{what} {text!r} carries a time zone but the event's times are floating; pass tz=")
        assert spec.tz is not None
        return to_zone(v, spec.tz).replace(tzinfo=None)
    return v


# --- low-level editing ------------------------------------------------------------


def detect_newline(text: str) -> str:
    for phys in split_physical(text):
        if phys.endswith("\r\n"):
            return "\r\n"
        if phys.endswith("\n"):
            return "\n"
        if phys.endswith("\r"):
            return "\r"
    return "\r\n"


def _fold_all(logical: list[str], nl: str) -> list[str]:
    out: list[str] = []
    for ln in logical:
        for piece in fold_line(ln):
            out.append(piece + nl)
    return out


def _root(cal: CalendarFile) -> Component:
    for r in cal.parsed.roots:
        if r.name == "VCALENDAR":
            return r
    raise CalendarError(f"{cal.rel}: no VCALENDAR")  # unreachable: parse() requires one


def splice(
    lines: list[str],
    remove: list[tuple[int, int]],
    insert: dict[int, list[str]],
) -> list[str]:
    """New physical line list: skip the (first, last) ranges in `remove`,
    and put `insert[i]` before original line i."""
    skip: set[int] = set()
    for a, b in remove:
        skip.update(range(a, b + 1))
    out: list[str] = []
    for i, ln in enumerate(lines):
        if i in insert:
            out.extend(insert[i])
        if i not in skip:
            out.append(ln)
    if len(lines) in insert:
        out.extend(insert[len(lines)])
    return out


def _validate(new_text: str, rel: str, uid: str | None, expect_present: bool) -> None:
    """Refuse to produce a file that no longer parses or lost/gained the event."""
    try:
        parsed = parse(new_text)
    except CalendarError as e:
        raise CalendarError(f"internal check failed, nothing written: edited {rel} would be invalid ({e})") from e
    if uid is None:
        return
    found = False
    for root in parsed.roots:
        for ev in root.sub("VEVENT"):
            p = ev.get("UID")
            if p is not None and p.value.strip() == uid:
                found = True
    if found != expect_present:
        state = "missing" if expect_present else "still present"
        raise CalendarError(f"internal check failed, nothing written: event {uid} would be {state}")


def make_diff(old: str, new: str, rel: str, newrel: str) -> str:
    a = [x.rstrip("\r\n") for x in split_physical(old)]
    b = [x.rstrip("\r\n") for x in split_physical(new)]
    return "\n".join(difflib.unified_diff(a, b, fromfile=rel, tofile=newrel, lineterm="", n=2))


# --- create -------------------------------------------------------------------------


@dataclass
class EventFields:
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    duration_minutes: int | None = None
    tz: str | None = None
    location: str | None = None
    description: str | None = None
    attendees: list[str] | None = None
    rrule: str | None = None
    status: str | None = None
    transp: str | None = None


def _check_address(a: str) -> str:
    s = a.strip()
    if s.lower().startswith("mailto:"):
        s = s[7:]
    if not re.fullmatch(r"[^\s@;:,\"<>]+@[^\s@;:,\"<>]+", s):
        raise CalendarError(f"attendee {a!r} is not an email address like name@example.com")
    return s


def _duration_text(minutes: int, all_day: bool) -> str:
    if minutes <= 0:
        raise CalendarError("duration_minutes must be positive")
    if all_day:
        if minutes % 1440:
            raise CalendarError("an all-day event's duration must be whole days (a multiple of 1440 minutes)")
        return f"P{minutes // 1440}D"
    h, m = divmod(minutes, 60)
    return "PT" + (f"{h}H" if h else "") + (f"{m}M" if m else "")


def _check_choice(value: str, allowed: tuple[str, ...], what: str) -> str:
    v = value.strip().upper()
    if v not in allowed:
        raise CalendarError(f"{what} must be one of {', '.join(allowed)}, got {value!r}")
    return v


def new_calendar_text(nl: str = "\r\n") -> str:
    return nl.join(["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", ""])


def build_create(
    base_text: str, root: Component | None, f: EventFields, now: datetime
) -> tuple[str, str, list[str]]:
    """Returns (new file text, uid, notes)."""
    if not f.summary or not f.summary.strip():
        raise CalendarError("summary is required")
    if not f.start:
        raise CalendarError("start is required (2026-03-02 for all-day, 2026-03-02T09:30 for timed)")
    spec = ZoneSpec.of(f.tz or "UTC")
    start = _input_value(f.start, spec, "start")
    all_day = not isinstance(start, datetime)
    notes: list[str] = []
    lines: list[str] = []
    uid = f"{uuid.uuid4()}@icskit-mcp"
    lines.append(f"UID:{uid}")
    lines.append(f"DTSTAMP:{format_utc(now)}")
    ln, n = time_line("DTSTART", start, spec)
    lines.append(ln)
    notes += n
    if f.end is not None and f.duration_minutes is not None:
        raise CalendarError("give end or duration_minutes, not both")
    if all_day:
        if f.end is not None:
            last = _input_value(f.end, spec, "end")
            if isinstance(last, datetime):
                raise CalendarError("an all-day event's end must be a date (the last day, inclusive)")
            if last < start:
                raise CalendarError("end is before start")
            excl: date | datetime = last + timedelta(days=1)
        else:
            days = f.duration_minutes or 1440
            if days % 1440:
                raise CalendarError("an all-day event's duration must be whole days (a multiple of 1440 minutes)")
            excl = start + timedelta(days=days // 1440)
        ln, n = time_line("DTEND", excl, spec)
        lines.append(ln)
    else:
        assert isinstance(start, datetime)
        if f.end is not None:
            end = _input_value(f.end, spec, "end")
            if not isinstance(end, datetime):
                raise CalendarError("a timed event's end must include a time")
            if spec.tz is not None and place(end, spec.tz).astimezone(UTC) <= place(start, spec.tz).astimezone(UTC):
                raise CalendarError("end must be after start")
            if spec.tz is None and end <= start:
                raise CalendarError("end must be after start")
            ln, n = time_line("DTEND", end, spec)
            lines.append(ln)
            notes += n
        elif f.duration_minutes is not None:
            lines.append(f"DURATION:{_duration_text(f.duration_minutes, False)}")
        else:
            raise CalendarError("give end or duration_minutes for a timed event")
    lines.append(f"SUMMARY:{escape_text(_check_text(f.summary.strip(), 'summary'))}")
    if f.location:
        lines.append(f"LOCATION:{escape_text(_check_text(f.location, 'location'))}")
    if f.description:
        lines.append(f"DESCRIPTION:{escape_text(_check_text(f.description, 'description'))}")
    if f.status:
        lines.append(f"STATUS:{_check_choice(f.status, _STATUSES, 'status')}")
    if f.transp:
        lines.append(f"TRANSP:{_check_choice(f.transp, _TRANSPS, 'transp')}")
    if f.rrule:
        _check_text(f.rrule, "rrule")
        text = f.rrule.strip()
        if text.upper().startswith("RRULE:"):
            text = text[6:]
        parse_rule(text)
        lines.append(f"RRULE:{text}")
    for a in f.attendees or []:
        lines.append(f"ATTENDEE:mailto:{_check_address(a)}")

    block = ["BEGIN:VEVENT", *lines, "END:VEVENT"]
    if root is None:
        nl = "\r\n"
        text = base_text if base_text else new_calendar_text(nl)
        out = text + "".join(_fold_all(block, nl)) + nl.join(["END:VCALENDAR", ""])
        return out, uid, notes
    nl = detect_newline(base_text)
    phys = split_physical(base_text)
    new_phys = splice(phys, [], {root.last: _fold_all(block, nl)})
    return "".join(new_phys), uid, notes


# --- update ---------------------------------------------------------------------------


def _event_zone(ev: Event) -> ZoneSpec:
    if ev.start.kind == "utc":
        return ZoneSpec("utc", "UTC", UTC)
    if ev.start.kind == "zoned" and ev.start.tzid:
        z = ev.start.tz
        key = getattr(z, "key", None)
        if isinstance(key, str):
            return ZoneSpec("zoned", key, z)
        # a VTIMEZONE-defined zone: keep writing its TZID verbatim
        return ZoneSpec("zoned", ev.start.tzid, z)
    if ev.start.kind == "floating":
        return FLOATING
    return ZoneSpec("utc", "UTC", UTC)


def build_update(
    cal: CalendarFile, series: Series, f: EventFields, now: datetime
) -> tuple[str, list[str]]:
    master = series.master
    if master is None:
        raise CalendarError(
            f"event {series.uid} only has modified instances in this file (no master event); "
            f"it cannot be updated safely"
        )
    comp = master.comp
    nl = detect_newline(cal.text)
    notes: list[str] = []
    remove: list[tuple[int, int]] = []
    insert: dict[int, list[str]] = {}

    def replace(name: str, new_lines: list[str]) -> None:
        old = comp.get_all(name)
        for p in old:
            remove.append((p.first, p.last))
        at = old[0].first if old else comp.last
        if new_lines:
            insert.setdefault(at, []).extend(_fold_all(new_lines, nl))

    def text_field(name: str, value: str | None) -> None:
        if value is None:
            return
        replace(name, [f"{name}:{escape_text(_check_text(value, name.lower()))}"] if value else [])

    text_field("SUMMARY", f.summary)
    if f.summary is not None and not f.summary.strip():
        raise CalendarError("summary cannot be empty")
    text_field("LOCATION", f.location)
    text_field("DESCRIPTION", f.description)
    if f.status is not None:
        replace("STATUS", [f"STATUS:{_check_choice(f.status, _STATUSES, 'status')}"] if f.status else [])
    if f.transp is not None:
        replace("TRANSP", [f"TRANSP:{_check_choice(f.transp, _TRANSPS, 'transp')}"] if f.transp else [])
    if f.rrule is not None:
        if f.rrule.strip():
            _check_text(f.rrule, "rrule")
            t = f.rrule.strip()
            t = t[6:] if t.upper().startswith("RRULE:") else t
            parse_rule(t)
            replace("RRULE", [f"RRULE:{t}"])
        else:
            replace("RRULE", [])

    if f.attendees is not None:
        want = {_check_address(a).lower(): _check_address(a) for a in f.attendees}
        have: dict[str, Prop] = {}
        for p in comp.get_all("ATTENDEE"):
            addr = p.value.strip()
            addr = addr[7:] if addr.lower().startswith("mailto:") else addr
            have[addr.lower()] = p
        for addr, p in have.items():
            if addr not in want:
                remove.append((p.first, p.last))
        add = [f"ATTENDEE:mailto:{a}" for k, a in want.items() if k not in have]
        if add:
            at = comp.get_all("ATTENDEE")[-1].last + 1 if comp.get_all("ATTENDEE") else comp.last
            insert.setdefault(at, []).extend(_fold_all(add, nl))

    time_change = (
        f.start is not None or f.end is not None or f.duration_minutes is not None
    )
    if f.tz is not None and not time_change:
        raise CalendarError("tz only says how to read start/end; pass it together with start or end")
    if f.end is not None and f.duration_minutes is not None:
        raise CalendarError("give end or duration_minutes, not both")
    if time_change:
        spec = ZoneSpec.of(f.tz) if f.tz else _event_zone(master)
        old_all_day = master.all_day
        if f.start is not None:
            new_start = _input_value(f.start, spec, "start")
        else:
            new_start = master.start.wall
        new_all_day = not isinstance(new_start, datetime)
        if new_all_day != old_all_day and f.end is None and f.duration_minutes is None:
            raise CalendarError(
                "switching between an all-day and a timed event needs end or duration_minutes as well"
            )
        # DTSTART
        if f.start is not None:
            ln, n = time_line("DTSTART", new_start, spec)
            notes += n
            replace("DTSTART", [ln])
        # length
        if f.end is not None:
            new_end = _input_value(f.end, spec, "end")
            if new_all_day:
                if isinstance(new_end, datetime):
                    raise CalendarError("an all-day event's end must be a date (the last day, inclusive)")
                if new_end < new_start:
                    raise CalendarError("end is before start")
                excl: date | datetime = new_end + timedelta(days=1)
            else:
                if not isinstance(new_end, datetime):
                    raise CalendarError("a timed event's end must include a time")
                assert isinstance(new_start, datetime)
                z = spec.tz or UTC
                if place(new_end, z).astimezone(UTC) <= place(new_start, z).astimezone(UTC):
                    raise CalendarError("end must be after start")
                excl = new_end
            ln, n = time_line("DTEND", excl, spec)
            notes += n
            replace("DTEND", [ln])
            replace("DURATION", [])
        elif f.duration_minutes is not None:
            replace("DTEND", [])
            replace("DURATION", [f"DURATION:{_duration_text(f.duration_minutes, new_all_day)}"])
        elif f.start is not None and master.end is not None:
            # keep the event's length: shift DTEND by the same amount
            old_s, old_e = master.start, master.end
            if isinstance(new_start, datetime):
                assert isinstance(old_s.wall, datetime) and isinstance(old_e.wall, datetime)
                zone = spec.tz or UTC
                length = old_e.instant(UTC) - old_s.instant(UTC)
                new_end_dt = (place(new_start, zone).astimezone(UTC) + length).astimezone(zone)
                excl2: date | datetime = new_end_dt.replace(tzinfo=None)
                end_spec = spec
            else:
                assert isinstance(old_s.wall, date) and isinstance(old_e.wall, date)
                excl2 = new_start + (old_e.wall - old_s.wall)
                end_spec = spec
            ln, n = time_line("DTEND", excl2, end_spec)
            notes += n
            replace("DTEND", [ln])
        if series.overrides and f.start is not None:
            notes.append(
                f"this series has {len(series.overrides)} modified instance(s) (RECURRENCE-ID); "
                f"they keep their old original dates and may no longer line up"
            )

    if not remove and not insert:
        raise CalendarError("nothing to change: pass at least one field to update")
    # bookkeeping: bump SEQUENCE, refresh DTSTAMP
    replace("DTSTAMP", [f"DTSTAMP:{format_utc(now)}"])
    replace("SEQUENCE", [f"SEQUENCE:{master.sequence + 1}"])
    phys = split_physical(cal.text)
    new_phys = splice(phys, remove, insert)
    return "".join(new_phys), notes


# --- delete -----------------------------------------------------------------------------


def _find_instance(series: Series, when: date | datetime, tz: tzinfo) -> tuple[Event | None, str]:
    """Locate the instance of a recurring series that starts at `when`.
    Returns (override event or None, EXDATE content line)."""
    master = series.master
    if master is None or not master.recurring:
        raise CalendarError(
            f"event {series.uid} is not a recurring series; delete the whole event (omit occurrence)"
        )
    floating = tz
    if isinstance(when, datetime):
        centre = to_zone(when, tz)
        ws, we = centre - timedelta(days=2), centre + timedelta(days=2)
    else:
        centre = to_zone(when, tz)
        ws, we = centre - timedelta(days=2), centre + timedelta(days=3)
    plain = Series(series.uid, series.calendar, master, [])
    cands = expand_series(plain, ws, we, floating)
    hit = None
    for o in cands:
        if master.all_day:
            if isinstance(when, date) and not isinstance(when, datetime) and o.start == when:
                hit = o
            elif isinstance(when, datetime) and isinstance(o.start, date) and o.start == when.date():
                hit = o
        elif isinstance(when, datetime) and isinstance(o.start, datetime):
            if o.start.astimezone(UTC) == to_zone(when, tz).astimezone(UTC):
                hit = o
        elif isinstance(when, date) and isinstance(o.start, datetime):
            if o.start.astimezone(tz).date() == when:
                hit = o
    if hit is None:
        near = ", ".join(
            (o.start.astimezone(tz).strftime("%Y-%m-%d %H:%M") if isinstance(o.start, datetime) else str(o.start))
            for o in cands
        ) or "none within 2 days"
        raise CalendarError(
            f"the series {series.uid} has no occurrence starting at {when}. "
            f"Occurrences nearby (zone {getattr(tz, 'key', tz)}): {near}"
        )
    assert isinstance(hit.event, Event)
    spec = _event_zone(master)
    if master.all_day:
        assert isinstance(hit.start, date)
        wall: date | datetime = hit.start
    else:
        assert isinstance(hit.start, datetime)
        z = spec.tz or tz
        wall = hit.start.astimezone(z).replace(tzinfo=None)
        if spec.kind == "floating":
            wall = hit.start.astimezone(tz).replace(tzinfo=None)
    ln, _ = time_line("EXDATE", wall, spec)
    # find a matching override to delete as well
    key = value_key(wall, spec.tz)
    ov: Event | None = None
    for e in series.overrides:
        if e.recurrence_id is not None and e.recurrence_id.key() == key:
            ov = e
    return ov, ln


def build_delete(
    cal: CalendarFile, series: Series, occurrence: str | None, tz_name: str
) -> tuple[str, str]:
    """Returns (new text, human description)."""
    nl = detect_newline(cal.text)
    phys = split_physical(cal.text)
    remove: list[tuple[int, int]] = []
    insert: dict[int, list[str]] = {}
    if occurrence is None:
        for ev in series.events:
            remove.append((ev.comp.first, ev.comp.last))
        n = len(series.events)
        what = f"deleted event {series.uid}" + (f" and its {n - 1} modified instance(s)" if n > 1 else "")
        return "".join(splice(phys, remove, insert)), what
    tz = require_zone(tz_name)
    when = parse_when(occurrence, "occurrence")
    ov, exline = _find_instance(series, when, tz)
    master = series.master
    assert master is not None
    if ov is not None:
        remove.append((ov.comp.first, ov.comp.last))
    last_ex = master.comp.get_all("EXDATE")
    at = last_ex[-1].last + 1 if last_ex else master.comp.last
    insert.setdefault(at, []).extend(_fold_all([exline], nl))
    what = f"removed one occurrence of {series.uid} (added {exline})"
    return "".join(splice(phys, remove, insert)), what


# --- commit -------------------------------------------------------------------------------


@dataclass
class CommitResult:
    message: str
    diff: str
    wrote: Path | None


def commit(
    lib: Library,
    cal: CalendarFile | None,
    rel: str,
    new_text: str,
    in_place: bool,
    output_file: str | None,
    dry_run: bool,
    uid: str | None,
    expect_present: bool,
) -> CommitResult:
    """Validate and (unless dry_run) write the edited text."""
    old_text = cal.text if cal is not None else ""
    _validate(new_text, rel, uid, expect_present)
    if cal is None:
        target = lib.resolve(rel)
        if not rel.lower().endswith(".ics"):
            raise CalendarError("a new calendar file name must end in .ics")
        diff = make_diff("", new_text, "/dev/null", rel)
        if dry_run:
            return CommitResult(f"dry run: would create new calendar {rel}", diff, None)
        _write_new(target, new_text)
        lib.forget(target)
        return CommitResult(f"created new calendar {rel}", diff, target)
    original = cal.path
    if in_place:
        if output_file:
            raise CalendarError("in_place=true writes over the original; do not also pass output_file")
        diff = make_diff(old_text, new_text, rel, rel)
        if dry_run:
            return CommitResult(f"dry run: would modify {rel} in place", diff, None)
        if original.read_bytes() != old_text.encode("utf-8"):
            raise CalendarError(f"{rel} changed on disk since it was read; nothing written, please retry")
        _atomic_replace(original, new_text)
        lib.forget(original)
        return CommitResult(f"modified {rel} IN PLACE", diff, original)
    if output_file:
        target = lib.resolve(output_file)
        if target == original:
            raise CalendarError("output_file is the original file; pass in_place=true if you really mean to overwrite it")
    else:
        target = original.with_name(original.name + ".new")
    trel = lib.rel(target)
    diff = make_diff(old_text, new_text, rel, trel)
    if dry_run:
        return CommitResult(f"dry run: would write the edited copy to {trel} (original untouched)", diff, None)
    if target.exists():
        raise CalendarError(
            f"{trel} already exists; refusing to overwrite it. Pass a different output_file, "
            f"delete it, or use in_place=true"
        )
    _write_new(target, new_text)
    lib.forget(target)
    return CommitResult(f"wrote edited copy to {trel}; the original {rel} is untouched", diff, target)


def _write_new(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "xb") as fh:
            fh.write(text.encode("utf-8"))
    except FileExistsError:
        raise CalendarError(f"{target.name} already exists; refusing to overwrite it") from None


def _atomic_replace(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
