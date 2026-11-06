"""icskit-mcp: an MCP server for local .ics calendar files."""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import writer
from .ical import CalendarError
from .library import CalendarFile, Library, series_extent
from .model import Event, Occurrence, Series, expand_series, sort_occurrences
from .timeutil import (
    busy_intervals,
    find_overlaps,
    fmt_dt,
    fmt_duration,
    fmt_offset,
    fmt_time,
    make_window,
    parse_days,
    parse_when,
    parse_working_hours,
    subtract,
    to_zone,
    working_intervals,
    zone_name,
)
from .tz import UTC, require_zone

mcp = MCPServer(
    "icskit",
    instructions=(
        "Work with LOCAL .ics calendar files (exports from Google, Apple, Outlook, or a folder "
        "of .ics) under the directory the server was started with. No accounts, no network. "
        "Start with calendar_overview. ALWAYS pass tz (an IANA name such as Europe/Istanbul): "
        "every time in every output is shown in that zone and labelled with it. Read tools: "
        "agenda, search_events, find_free_time, find_conflicts, next_occurrences, "
        "export_agenda_markdown. Write tools (create_event, update_event, delete_event) never "
        "modify the original file by default: they write an edited copy (<file>.new) or, with "
        "dry_run=true, only show the patch; pass in_place=true to change the original."
    ),
)

_lib: Library | None = None

F = TypeVar("F", bound=Callable[..., Any])

_EXPECTED = (CalendarError, OSError)


def tool(fn: F) -> F:
    """Register fn as a tool; expected failures become ToolError so the agent
    sees the message (mcp>=2 hides every other exception type)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


_WRITE_NOTE = (
    "SAFETY: the original file is never modified unless in_place=true. By default the edited "
    "copy is written next to it as '<file>.new' (or output_file, which must not exist yet). "
    "dry_run=true writes nothing and just shows the patch. Everything else in the file - other "
    "events, VTIMEZONE blocks, unknown properties - is kept byte for byte."
)


def write_tool(fn: F) -> F:
    """Like tool, but appends the shared safety paragraph to the description."""
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n\n" + _WRITE_NOTE
    return tool(fn)


def configure(root: str | Path) -> Library:
    global _lib
    p = Path(root)
    if not p.is_dir():
        raise CalendarError(f"--dir {root!r} is not a directory")
    _lib = Library(p)
    return _lib


def lib() -> Library:
    if _lib is None:
        raise CalendarError("server is not configured: start it with --dir <folder with .ics files>")
    return _lib


# --- formatting helpers ------------------------------------------------------------------


def _day_label(d: date) -> str:
    return d.strftime("%a %Y-%m-%d")


def _one_line(text: str) -> str:
    return " / ".join(p.strip() for p in text.splitlines() if p.strip())


def _occ_line(o: Occurrence, tz: tzinfo, ws: datetime | None = None, dated: bool = False) -> str:
    ev = o.event
    if o.all_day:
        assert isinstance(o.start, date) and isinstance(o.end, date)
        last = o.end - timedelta(days=1)
        when = "all day" if last <= o.start else f"all day ({o.start} .. {last})"
        if dated and last <= o.start:
            when = f"{o.start} all day"
    else:
        assert isinstance(o.start, datetime) and isinstance(o.end, datetime)
        s, e = o.start.astimezone(tz), o.end.astimezone(tz)
        if o.span(tz)[0] == o.span(tz)[1]:
            when = f"{fmt_time(s, tz)} {s.tzname()}"
        elif e.date() == s.date():
            when = f"{fmt_time(s, tz)}-{fmt_time(e, tz)} {e.tzname()}"
        else:
            when = f"{fmt_time(s, tz)} {s.tzname()} -> {fmt_dt(e, tz)}"
        if ws is not None and s < ws:
            when += " (started earlier)"
        if dated:
            when = s.strftime("%Y-%m-%d ") + when
    tags = []
    if o.override:
        tags.append("modified instance")
    elif o.recurring:
        tags.append("recurring")
    if ev.cancelled:
        tags.append("CANCELLED")
    if ev.transparent and not o.all_day:
        tags.append("shown as free")
    tag = f" [{', '.join(tags)}]" if tags else ""
    loc = f" @ {_one_line(ev.location)}" if ev.location else ""
    return f"{when}  {_one_line(ev.title())}{tag}{loc}  ({ev.calendar}, uid {ev.uid})"


def _warn_block(max_items: int = 5) -> str:
    w = lib().all_warnings()
    if not w:
        return ""
    shown = w[:max_items]
    more = f"\n  ... and {len(w) - max_items} more (see calendar_overview)" if len(w) > max_items else ""
    return "\nWarnings:\n  " + "\n  ".join(shown) + more + "\n"


def _group_by_day(occs: list[Occurrence], tz: tzinfo, ws: datetime) -> dict[date, list[Occurrence]]:
    days: dict[date, list[Occurrence]] = {}
    for o in occs:
        s, _ = o.span(tz)
        d = max(s, ws).astimezone(tz).date()
        days.setdefault(d, []).append(o)
    return days


def _agenda_text(occs: list[Occurrence], tz: tzinfo, ws: datetime, we: datetime, limit: int) -> list[str]:
    lines: list[str] = []
    count = 0
    for d, items in sorted(_group_by_day(occs, tz, ws).items()):
        lines.append("")
        lines.append(_day_label(d))
        for o in items:
            if count >= limit:
                break
            lines.append("  " + _occ_line(o, tz, ws))
            count += 1
    if count < len(occs):
        lines.append("")
        lines.append(f"... {len(occs) - count} more occurrence(s) not shown; narrow the range or raise limit")
    return lines


# --- read tools ----------------------------------------------------------------------------


@tool
def calendar_overview() -> str:
    """List the calendar files the server can see: for each file its name, how many events
    and recurring series it holds, the date span of its events (as written in the file) and
    the time zones its events use. Also lists files that failed to load and any events that
    were skipped, with the reason. Call this first; the file names it prints are what the
    `calendars` and `calendar` parameters of the other tools accept."""
    L = lib()
    files = L.files()
    out = [f"Directory: {L.root}"]
    if not files and not L.errors:
        out.append("No .ics files found under this directory.")
    total_series = sum(len(f.series) for f in files)
    total_events = sum(len(f.events) for f in files)
    out.append(f"{len(files)} calendar file(s), {total_series} event(s) / series, {total_events} VEVENT component(s)")
    for f in files:
        recurring = sum(1 for s in f.series.values() if s.master is not None and s.master.recurring)
        mods = sum(len(s.overrides) for s in f.series.values())
        firsts: list[date] = []
        lasts: list[date] = []
        open_ended = 0
        for s in f.series.values():
            a, b, oe = series_extent(s)
            if a is not None:
                firsts.append(a.date() if isinstance(a, datetime) else a)
            if b is not None:
                lasts.append(b.date() if isinstance(b, datetime) else b)
            open_ended += 1 if oe else 0
        span = "no events"
        if firsts and lasts:
            span = f"{min(firsts)} .. {max(lasts)}"
            if open_ended:
                span += f" (and {open_ended} series that repeat with no end)"
        label = f'"{f.name}" ' if f.name != f.path.stem else ""
        out.append(f"- {f.rel} {label}: {len(f.series)} events, {recurring} recurring, {mods} modified instances")
        out.append(f"    dates (as written in the file): {span}")
        out.append(f"    time zones: {', '.join(sorted(f.tzids)) or 'none'}")
    problems = L.all_warnings()
    if problems:
        out.append("")
        out.append(f"Problems ({len(problems)}):")
        out.extend(f"  {p}" for p in problems[:50])
        if len(problems) > 50:
            out.append(f"  ... and {len(problems) - 50} more")
    return "\n".join(out)


@tool
def agenda(
    start: str,
    end: str,
    tz: str = "UTC",
    calendars: list[str] | None = None,
    include_cancelled: bool = False,
    limit: int = 300,
) -> str:
    """Show every event occurrence between start and end, sorted, grouped by day, with
    recurring events already expanded (RRULE, EXDATE, RDATE and modified instances applied).

    start/end: '2026-03-02' or '2026-03-02T09:30' (optionally with Z or an offset). A
    date-only `end` includes that whole day. tz: IANA zone name used to read start/end and to
    show every time; the zone is printed in the header and next to each time. Floating times
    (no zone in the file) are read in `tz`. All-day events show as 'all day' and are not
    shifted by tz. calendars: file names from calendar_overview (default: all).
    Cancelled events are hidden unless include_cancelled=true."""
    ws, we, z = make_window(start, end, tz)
    occs = lib().occurrences(ws, we, z, calendars, include_cancelled)
    head = f"Agenda {fmt_dt(ws, z)} -> {fmt_dt(we, z)}  (zone {zone_name(z)}, UTC{fmt_offset(ws, z)} at start)"
    if not occs:
        return head + "\nNo events in this range." + _warn_block()
    body = _agenda_text(occs, z, ws, we, limit)
    return head + f"\n{len(occs)} occurrence(s)" + "\n".join([""] + body) + _warn_block()


def _searchable(ev: Event) -> str:
    parts = [ev.summary, ev.description, ev.location, " ".join(ev.categories)]
    for a in ev.attendees:
        parts.append(f"{a.name or ''} {a.address}")
    if ev.organizer is not None:
        parts.append(f"{ev.organizer.name or ''} {ev.organizer.address}")
    return "\n".join(parts)


def _matches(ev: Event, query: str | None, attendee: str | None, location: str | None, rx: re.Pattern[str] | None) -> bool:
    if query and query.lower() not in _searchable(ev).lower():
        return False
    if attendee:
        a = attendee.lower()
        people = [p for p in ev.attendees] + ([ev.organizer] if ev.organizer else [])
        if not any(a in p.address.lower() or a in (p.name or "").lower() for p in people):
            return False
    if location and location.lower() not in ev.location.lower():
        return False
    if rx is not None and not rx.search(_searchable(ev)):
        return False
    return True


@tool
def search_events(
    query: str | None = None,
    attendee: str | None = None,
    location: str | None = None,
    regex: str | None = None,
    start: str | None = None,
    end: str | None = None,
    tz: str = "UTC",
    calendars: list[str] | None = None,
    limit: int = 50,
) -> str:
    """Find events by text. All the filters you give must match (AND).

    query: case-insensitive text found in the title, description, location, categories or
    attendees. attendee: part of an attendee/organizer email or name. location: part of the
    location. regex: a Python regular expression (case-insensitive; ^ and $ match at the start and end of each field) searched in the same fields.
    Without start/end this lists each matching event or recurring series once (first start,
    recurrence rule, UID). With start AND end it lists the matching OCCURRENCES in that range,
    expanded, like agenda. tz sets the zone used to show times."""
    if not any([query, attendee, location, regex]):
        raise CalendarError("give at least one of query, attendee, location or regex")
    rx: re.Pattern[str] | None = None
    if regex:
        if len(regex) > 300:
            raise CalendarError("regex is longer than 300 characters")
        try:
            rx = re.compile(regex, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            raise CalendarError(f"invalid regex {regex!r}: {e}") from e
    z = require_zone(tz)
    L = lib()
    if (start is None) != (end is None):
        raise CalendarError("pass both start and end, or neither")
    if start is not None and end is not None:
        ws, we, z = make_window(start, end, tz)
        occs = [o for o in L.occurrences(ws, we, z, calendars) if _matches(o.event, query, attendee, location, rx)]
        head = f"{len(occs)} matching occurrence(s) {fmt_dt(ws, z)} -> {fmt_dt(we, z)} (zone {zone_name(z)})"
        if not occs:
            return head + _warn_block()
        return head + "\n".join([""] + _agenda_text(occs, z, ws, we, limit)) + _warn_block()
    hits: list[tuple[CalendarFile, Series, Event]] = []
    for f in L.files(calendars):
        for s in f.series.values():
            for ev in s.events:
                if _matches(ev, query, attendee, location, rx):
                    hits.append((f, s, ev))
    lines = [f"{len(hits)} matching event(s) (times shown in {zone_name(z)})"]
    for f, s, ev in hits[:limit]:
        if ev.all_day:
            when = f"{ev.start.wall} (all day)"
        else:
            when = fmt_dt(ev.start.aware(z), z)
        extras = []
        if ev.rule is not None:
            r = ev.comp.get("RRULE")
            extras.append(f"repeats: {r.value if r else ''}")
        elif ev.rdates:
            extras.append("repeats: RDATE list")
        if ev.recurrence_id is not None:
            extras.append("modified instance")
        if ev.cancelled:
            extras.append("CANCELLED")
        if ev.location:
            extras.append(f"@ {_one_line(ev.location)}")
        if ev.attendees:
            extras.append(f"{len(ev.attendees)} attendee(s): " + ", ".join(a.label() for a in ev.attendees[:4]) + ("..." if len(ev.attendees) > 4 else ""))
        lines.append(f"- {when}  {_one_line(ev.title())}  ({f.rel}, uid {ev.uid})" + ("".join(f"\n    {x}" for x in extras)))
    if len(hits) > limit:
        lines.append(f"... {len(hits) - limit} more; refine the search or raise limit")
    return "\n".join(lines) + _warn_block()


@tool
def find_free_time(
    start: str,
    end: str,
    duration_minutes: int,
    tz: str = "UTC",
    working_hours: str = "09:00-17:00",
    days: str = "Mon-Fri",
    calendars: list[str] | None = None,
    buffer_minutes: int = 0,
    include_all_day: bool = False,
    include_transparent: bool = False,
    limit: int = 100,
) -> str:
    """Find free slots of at least duration_minutes inside working hours, across one or
    several calendars at once. Returns every maximal free block (not chopped into
    duration-sized pieces), so a 3h gap is reported as one 3h block.

    start/end/tz: as in agenda (date-only end includes the whole day). working_hours:
    local wall-clock ranges in tz, e.g. '09:00-17:00' or '09:00-12:00,13:00-17:00' (so DST
    days keep their wall hours). days: working weekdays, e.g. 'Mon-Fri' or 'Mon,Wed,Fri'.
    buffer_minutes: padding kept clear before AND after every busy event. Busy means an
    uncancelled event; all-day events and events marked TRANSP:TRANSPARENT ('free') do
    not block unless include_all_day / include_transparent is true."""
    if duration_minutes <= 0:
        raise CalendarError("duration_minutes must be positive")
    if buffer_minutes < 0:
        raise CalendarError("buffer_minutes cannot be negative")
    ws, we, z = make_window(start, end, tz)
    hours = parse_working_hours(working_hours)
    day_set = parse_days(days)
    buf = timedelta(minutes=buffer_minutes)
    pad = buf + timedelta(days=1)
    L = lib()
    files = L.files(calendars)
    occs = L.occurrences(ws - pad, we + pad, z, calendars)
    busy = busy_intervals(occs, z, buf, include_all_day, include_transparent)
    work = working_intervals(ws, we, z, hours, day_set)
    free = subtract(work, busy)
    need = timedelta(minutes=duration_minutes)
    slots = [(s, e) for s, e in free if e - s >= need]
    head = (
        f"Free slots >= {fmt_duration(need)} between {fmt_dt(ws, z)} and {fmt_dt(we, z)}\n"
        f"zone {zone_name(z)}; working hours {working_hours} on {days}"
        + (f"; {buffer_minutes} min buffer around events" if buffer_minutes else "")
        + f"\ncalendars: {', '.join(f.rel for f in files) or '(none)'}; "
        f"{sum(1 for o in occs if not o.event.cancelled)} event occurrence(s) in or near the range considered"
    )
    if not slots:
        return head + "\nNo free slot of that length in the range." + _warn_block()
    lines = [head, f"{len(slots)} slot(s):"]
    cur_day: date | None = None
    for s, e in slots[:limit]:
        sl, el = s.astimezone(z), e.astimezone(z)
        if sl.date() != cur_day:
            cur_day = sl.date()
            lines.append(_day_label(cur_day))
        end_txt = fmt_time(el, z) if el.date() == sl.date() else fmt_dt(el, z)
        lines.append(f"  {fmt_time(sl, z)}-{end_txt} {el.tzname()}  ({fmt_duration(e - s)})")
    if len(slots) > limit:
        lines.append(f"... {len(slots) - limit} more slot(s) not shown")
    total = sum((e - s for s, e in slots), timedelta(0))
    lines.append(f"Total free in these slots: {fmt_duration(total)}")
    return "\n".join(lines) + _warn_block()


@tool
def find_conflicts(
    start: str,
    end: str,
    tz: str = "UTC",
    calendars: list[str] | None = None,
    include_all_day: bool = False,
    include_transparent: bool = False,
    limit: int = 100,
) -> str:
    """List pairs of events whose times overlap in the range, including across different
    calendars. Back-to-back events (one ends exactly when the next starts) are not conflicts.
    Cancelled events are ignored. All-day events and events marked TRANSP:TRANSPARENT ('free')
    are ignored unless include_all_day / include_transparent is true.
    start/end/tz as in agenda."""
    ws, we, z = make_window(start, end, tz)
    occs = lib().occurrences(ws, we, z, calendars)
    kept = [
        o
        for o in occs
        if not o.event.cancelled
        and (include_all_day or not o.all_day)
        and (include_transparent or not o.event.transparent)
    ]
    conflicts = find_overlaps(kept, z)
    conflicts.sort(key=lambda c: (c.start, c.end))
    head = f"Conflicts {fmt_dt(ws, z)} -> {fmt_dt(we, z)} (zone {zone_name(z)}); {len(kept)} event occurrence(s) checked"
    if not conflicts:
        return head + "\nNo conflicts." + _warn_block()
    lines = [head, f"{len(conflicts)} conflicting pair(s):"]
    for c in conflicts[:limit]:
        cross = " [different calendars]" if c.a.event.calendar != c.b.event.calendar else ""
        lines.append(f"- overlap {fmt_dt(c.start, z)} -> {fmt_time(c.end, z)} ({fmt_duration(c.end - c.start)}){cross}")
        lines.append("    A: " + _occ_line(c.a, z, dated=True))
        lines.append("    B: " + _occ_line(c.b, z, dated=True))
    if len(conflicts) > limit:
        lines.append(f"... {len(conflicts) - limit} more pair(s) not shown")
    return "\n".join(lines) + _warn_block()


@tool
def next_occurrences(
    event_uid: str,
    n: int = 5,
    after: str | None = None,
    tz: str = "UTC",
    calendar: str | None = None,
) -> str:
    """The next n occurrences of one event (by UID) at or after `after` (default: now),
    with modified instances and removed dates applied. Use it to check what a recurrence
    rule really produces, e.g. 'last Friday of the month'. UIDs come from agenda and
    search_events. calendar: file name, only needed if the UID exists in several files."""
    if not 1 <= n <= 500:
        raise CalendarError("n must be between 1 and 500")
    z = require_zone(tz)
    f, s = lib().find_series(event_uid, calendar)
    base = to_zone(parse_when(after, "after"), z) if after else datetime.now(UTC).astimezone(z)
    found: list[Occurrence] = []
    for years in (1, 5, 25, 100):
        found = sort_occurrences(expand_series(s, base, base + timedelta(days=366 * years), z), z)
        found = [o for o in found if o.span(z)[1] > base or o.span(z)[0] >= base]
        if len(found) >= n:
            break
    title = s.master.title() if s.master else s.events[0].title()
    head = f"Next occurrences of {title!r} ({f.rel}, uid {event_uid}) from {fmt_dt(base, z)} (zone {zone_name(z)})"
    if not found:
        return head + "\nNo further occurrences (the series has ended or has no instance in the next 100 years)."
    lines = [head]
    for o in found[:n]:
        lines.append("  " + _occ_line(o, z, dated=True))
    if len(found) < n:
        lines.append(f"Only {len(found)} occurrence(s) left in the next 100 years.")
    return "\n".join(lines)


@tool
def export_agenda_markdown(
    week_start: str,
    tz: str = "UTC",
    calendars: list[str] | None = None,
    output_file: str | None = None,
) -> str:
    """Render seven days starting at week_start (a date such as 2026-03-02) as Markdown: one
    heading per day with a bullet per event, times labelled with the zone. Returns the
    Markdown. With output_file (a new .md file name under the server directory) it is also
    saved; an existing file is never overwritten."""
    z = require_zone(tz)
    d0 = parse_when(week_start, "week_start")
    if not isinstance(d0, date) or isinstance(d0, datetime):
        raise CalendarError("week_start must be a date like 2026-03-02")
    ws = to_zone(d0, z)
    we = to_zone(d0 + timedelta(days=7), z)
    occs = lib().occurrences(ws, we, z, calendars)
    by_day = _group_by_day(occs, z, ws)
    md = [f"# Agenda {d0} to {d0 + timedelta(days=6)} ({zone_name(z)})", ""]
    for i in range(7):
        d = d0 + timedelta(days=i)
        md.append(f"## {_day_label(d)}")
        items = by_day.get(d, [])
        if not items:
            md.append("_No events_")
        for o in items:
            md.append("- " + _occ_line(o, z, ws).replace("  ", " - ", 1))
        md.append("")
    text = "\n".join(md)
    if output_file:
        target = lib().resolve(output_file)
        if target.suffix.lower() != ".md":
            raise CalendarError("output_file must end in .md")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "xb") as fh:
                fh.write(text.encode("utf-8"))
        except FileExistsError:
            raise CalendarError(f"{output_file} already exists; refusing to overwrite it") from None
        text += f"\n(saved to {lib().rel(target)})"
    return text


# --- write tools -----------------------------------------------------------------------------

def _report(res: writer.CommitResult, extra: list[str]) -> str:
    out = [res.message]
    out.extend(extra)
    if res.diff:
        out.append("")
        out.append("Patch:")
        out.append(res.diff)
    return "\n".join(out)


@write_tool
def create_event(
    calendar: str,
    summary: str,
    start: str,
    end: str | None = None,
    duration_minutes: int | None = None,
    tz: str = "UTC",
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    rrule: str | None = None,
    status: str | None = None,
    transp: str | None = None,
    in_place: bool = False,
    output_file: str | None = None,
    dry_run: bool = False,
) -> str:
    """Add an event to a calendar file. A UID and DTSTAMP are generated.

    calendar: file name from calendar_overview (a name that does not exist yet creates a
    new file; it must end in .ics). start: '2026-03-02' makes an ALL-DAY event (end, if
    given, is the LAST day, inclusive); '2026-03-02T09:30' makes a timed event, which needs
    end or duration_minutes. The times are read in tz and stored with that zone (a TZID
    reference; no VTIMEZONE block is added). rrule: e.g. 'FREQ=WEEKLY;BYDAY=MO,WE'.
    attendees: email addresses (recorded only; nothing is ever sent).
    status: TENTATIVE/CONFIRMED/CANCELLED. transp: OPAQUE (busy) or TRANSPARENT (free)."""
    L = lib()
    path = L.resolve(calendar)
    cal = L.get_file(calendar) if path.exists() else None
    if cal is None and output_file:
        raise CalendarError(f"{calendar} does not exist yet, so it is created directly; omit output_file")
    fields = writer.EventFields(
        summary=summary, start=start, end=end, duration_minutes=duration_minutes, tz=tz,
        location=location, description=description, attendees=attendees, rrule=rrule,
        status=status, transp=transp,
    )
    now = writer.utcnow()
    if cal is None:
        new_text, uid, notes = writer.build_create("", None, fields, now)
    else:
        root = next(r for r in cal.parsed.roots if r.name == "VCALENDAR")
        new_text, uid, notes = writer.build_create(cal.text, root, fields, now)
    res = writer.commit(L, cal, L.rel(path), new_text, in_place, output_file, dry_run, uid, True)
    return _report(res, [f"UID: {uid}", *notes])


@write_tool
def update_event(
    event_uid: str,
    calendar: str | None = None,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    duration_minutes: int | None = None,
    tz: str | None = None,
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    rrule: str | None = None,
    status: str | None = None,
    transp: str | None = None,
    in_place: bool = False,
    output_file: str | None = None,
    dry_run: bool = False,
) -> str:
    """Change fields of one event (the whole series if it recurs). Only the fields you pass
    change; an empty string removes summary-like fields (location, description, status,
    transp, rrule). attendees=[...] makes the attendee list exactly that (existing entries
    keep their details). start without end keeps the event's length. start/end are read in
    tz (default: the event's own zone); all-day rules as in create_event. DTSTAMP is
    refreshed and SEQUENCE incremented. Modifying single instances of a series is not
    supported. calendar: only needed if the UID is in several files."""
    L = lib()
    cal, series = L.find_series(event_uid, calendar)
    fields = writer.EventFields(
        summary=summary, start=start, end=end, duration_minutes=duration_minutes, tz=tz,
        location=location, description=description, attendees=attendees, rrule=rrule,
        status=status, transp=transp,
    )
    new_text, notes = writer.build_update(cal, series, fields, writer.utcnow())
    res = writer.commit(L, cal, cal.rel, new_text, in_place, output_file, dry_run, event_uid, True)
    return _report(res, notes)


@write_tool
def delete_event(
    event_uid: str,
    calendar: str | None = None,
    occurrence: str | None = None,
    tz: str = "UTC",
    in_place: bool = False,
    output_file: str | None = None,
    dry_run: bool = False,
) -> str:
    """Delete an event by UID (a recurring event with all its modified instances), or just
    one occurrence of a recurring event: pass occurrence='2026-03-09T09:00' (its start, read
    in tz; or a date for all-day) and an EXDATE is added instead."""
    L = lib()
    cal, series = L.find_series(event_uid, calendar)
    new_text, what = writer.build_delete(cal, series, occurrence, tz)
    res = writer.commit(L, cal, cal.rel, new_text, in_place, output_file, dry_run, event_uid, occurrence is not None)
    return _report(res, [what])


# --- entry point -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="icskit-mcp",
        description="MCP server for local .ics calendar files. Only files under --dir are visible.",
    )
    ap.add_argument(
        "--dir",
        default=os.environ.get("ICSKIT_MCP_DIR"),
        help="folder containing .ics files (searched recursively); or set ICSKIT_MCP_DIR",
    )
    args = ap.parse_args(argv)
    if not args.dir:
        ap.error("--dir is required (the folder with your .ics files)")
    try:
        configure(args.dir)
    except CalendarError as e:
        print(f"icskit-mcp: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    mcp.run()


if __name__ == "__main__":
    main()
