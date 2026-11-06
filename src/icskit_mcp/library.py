"""The calendar library: every .ics file under the sandbox directory, parsed
once and re-parsed only when the file changes on disk."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from .ical import CalendarError, ICalError, Parsed, parse, unescape_text
from .model import Event, Occurrence, Series, build_event, expand_series, sort_occurrences
from .rrule import Wall, iterate
from .tz import UTC, TzResolver

MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass
class CalendarFile:
    rel: str  # posix path relative to the sandbox directory
    path: Path
    name: str  # X-WR-CALNAME or the file stem
    text: str
    parsed: Parsed
    series: dict[str, Series]
    events: list[Event]
    warnings: list[str]
    tzids: set[str] = field(default_factory=set)
    stamp: tuple[int, int] = (0, 0)  # (mtime_ns, size)


def _load(path: Path, rel: str) -> CalendarFile:
    st = path.stat()
    if st.st_size > MAX_FILE_BYTES:
        raise CalendarError(f"{rel}: file is {st.st_size // (1024 * 1024)} MiB, over the {MAX_FILE_BYTES // (1024 * 1024)} MiB limit")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CalendarError(
            f"{rel}: not valid UTF-8 (byte {e.start}); re-export or re-save the calendar as UTF-8"
        ) from e
    try:
        parsed = parse(text)
    except ICalError as e:
        raise CalendarError(f"{rel}: {e}") from e

    warnings = [f"{rel}: {w}" for w in parsed.warnings]
    vtimezones = []
    vevents = []
    name = ""
    for root in parsed.roots:
        if root.name != "VCALENDAR":
            continue
        vtimezones.extend(root.sub("VTIMEZONE"))
        vevents.extend(root.sub("VEVENT"))
        cn = root.get("X-WR-CALNAME")
        if cn and not name:
            name = unescape_text(cn.value).strip()
    resolver = TzResolver(vtimezones)

    events: list[Event] = []
    series: dict[str, Series] = {}
    tzids: set[str] = set()
    for comp in vevents:
        try:
            ev = build_event(comp, rel, resolver)
        except CalendarError as e:
            summ = comp.get("SUMMARY")
            label = unescape_text(summ.value) if summ else "(no title)"
            warnings.append(f"{rel}: line {comp.first + 1}: event {label!r} skipped: {e}")
            continue
        events.append(ev)
        for tv in (ev.start, ev.end):
            if tv is not None:
                tzids.add("all-day" if tv.is_date else (tv.tzid or "floating"))
        s = series.get(ev.uid)
        if s is None:
            s = series[ev.uid] = Series(ev.uid, rel, None, [])
        if ev.recurrence_id is not None:
            rng = ev.comp.get("RECURRENCE-ID")
            if rng is not None and rng.param("RANGE"):
                warnings.append(
                    f"{rel}: line {comp.first + 1}: event {ev.title()!r}: RECURRENCE-ID RANGE="
                    f"{rng.param('RANGE')} is not supported; applied to a single instance only"
                )
            s.overrides.append(ev)
        elif s.master is None:
            s.master = ev
        else:
            warnings.append(
                f"{rel}: line {comp.first + 1}: duplicate UID {ev.uid!r}; the later copy is ignored"
            )
            events.pop()
    return CalendarFile(
        rel=rel,
        path=path,
        name=name or path.stem,
        text=text,
        parsed=parsed,
        series=series,
        events=events,
        warnings=warnings,
        tzids=tzids,
        stamp=(st.st_mtime_ns, st.st_size),
    )


class Library:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._files: dict[str, CalendarFile] = {}
        self.errors: dict[str, str] = {}
        self._err_stamp: dict[str, tuple[int, int]] = {}

    # -- paths --------------------------------------------------------------

    def resolve(self, name: str) -> Path:
        """Turn a user-supplied relative path into a path inside the sandbox."""
        if not name or not name.strip():
            raise CalendarError("calendar file name is empty")
        p = Path(name)
        full = (p if p.is_absolute() else self.root / p).resolve()
        if not full.is_relative_to(self.root):
            raise CalendarError(
                f"{name!r} is outside the allowed directory {self.root}; "
                f"the server only works with files under --dir"
            )
        return full

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    # -- loading ------------------------------------------------------------

    def refresh(self) -> None:
        found: dict[str, Path] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
            for fn in filenames:
                if fn.lower().endswith(".ics"):
                    full = Path(dirpath) / fn
                    try:
                        resolved = full.resolve()
                    except OSError:
                        continue
                    if resolved.is_relative_to(self.root):
                        found[resolved.relative_to(self.root).as_posix()] = resolved
        for gone in set(self._files) - set(found):
            del self._files[gone]
        for gone in set(self.errors) - set(found):
            del self.errors[gone]
        for rel, path in sorted(found.items()):
            st = path.stat()
            cur = self._files.get(rel)
            if cur is not None and cur.stamp == (st.st_mtime_ns, st.st_size):
                continue
            if rel in self.errors and self._err_stamp.get(rel) == (st.st_mtime_ns, st.st_size):
                continue
            try:
                self._files[rel] = _load(path, rel)
                self.errors.pop(rel, None)
            except CalendarError as e:
                self._files.pop(rel, None)
                self.errors[rel] = str(e)
                self._err_stamp[rel] = (st.st_mtime_ns, st.st_size)

    def forget(self, path: Path) -> None:
        """Drop the cached parse of a file this process just wrote, so the next
        query re-reads it even if the clock's mtime resolution hides the change."""
        try:
            rel = self.rel(path)
        except ValueError:
            return
        self._files.pop(rel, None)
        self.errors.pop(rel, None)
        self._err_stamp.pop(rel, None)

    def files(self, only: list[str] | None = None) -> list[CalendarFile]:
        """Loaded files, optionally restricted by relative path, stem or name."""
        self.refresh()
        allf = [self._files[k] for k in sorted(self._files)]
        if not only:
            return allf
        picked: list[CalendarFile] = []
        for want in only:
            w = want.strip().lower()
            hit = [
                f
                for f in allf
                if w in (f.rel.lower(), f.path.stem.lower(), f.name.lower(), Path(f.rel).name.lower())
            ]
            if not hit:
                avail = ", ".join(f.rel for f in allf) or "(none)"
                raise CalendarError(f"no calendar matches {want!r}. Available: {avail}")
            for f in hit:
                if f not in picked:
                    picked.append(f)
        return picked

    def get_file(self, name: str) -> CalendarFile:
        full = self.resolve(name)
        self.refresh()
        rel = self.rel(full)
        f = self._files.get(rel)
        if f is None:
            if rel in self.errors:
                raise CalendarError(self.errors[rel])
            raise CalendarError(
                f"{name!r} is not a calendar file under {self.root}. "
                f"Call calendar_overview to list the available files."
            )
        return f

    def all_warnings(self) -> list[str]:
        out: list[str] = []
        for f in self.files():
            out.extend(f.warnings)
        out.extend(f"{rel}: {msg}" if not msg.startswith(rel) else msg for rel, msg in self.errors.items())
        return out

    def find_series(self, uid: str, calendar: str | None = None) -> tuple[CalendarFile, Series]:
        pool = [self.get_file(calendar)] if calendar else self.files()
        hits = [(f, f.series[uid]) for f in pool if uid in f.series]
        if not hits:
            where = f" in {calendar}" if calendar else ""
            raise CalendarError(
                f"no event with UID {uid!r}{where}. Use agenda or search_events to find the UID."
            )
        if len(hits) > 1:
            names = ", ".join(f.rel for f, _ in hits)
            raise CalendarError(
                f"UID {uid!r} exists in several files ({names}); pass calendar= to choose one"
            )
        return hits[0]

    # -- queries ------------------------------------------------------------

    def occurrences(
        self,
        ws: datetime,
        we: datetime,
        qtz: tzinfo,
        calendars: list[str] | None = None,
        include_cancelled: bool = False,
    ) -> list[Occurrence]:
        out: list[Occurrence] = []
        for f in self.files(calendars):
            for s in f.series.values():
                out.extend(expand_series(s, ws, we, qtz, include_cancelled))
        return sort_occurrences(out, qtz)


def series_extent(series: Series) -> tuple[Wall | None, Wall | None, bool]:
    """(earliest wall start, latest wall end of finite series, open_ended)."""
    first: Wall | None = None
    last: Wall | None = None
    open_ended = False
    for ev in series.events:
        w = ev.start.wall
        if first is None or _cmp(w) < _cmp(first):
            first = w
        end_w: Wall = ev.end.wall if ev.end is not None else w
        if ev.duration is not None and isinstance(w, datetime):
            end_w = w + timedelta(days=ev.duration.days, seconds=ev.duration.seconds)
        if ev.rule is not None and ev.recurrence_id is None:
            r = ev.rule
            if r.until is not None:
                tail: Wall = r.until
            elif r.count is not None and r.count <= 5000:
                tail = w
                for tail in iterate(r, w):
                    pass
            else:
                open_ended = True
                tail = w
            delta = _cmp(end_w) - _cmp(w)
            end_w = tail + delta
        if last is None or _cmp(end_w) > _cmp(last):
            last = end_w
    return first, last, open_ended


def _cmp(w: Wall) -> datetime:
    if isinstance(w, datetime):
        return w
    return datetime(w.year, w.month, w.day)


def tz_label(tz: tzinfo) -> str:
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    if tz is UTC:
        return "UTC"
    return str(tz)

