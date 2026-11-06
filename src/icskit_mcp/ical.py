"""iCalendar (RFC 5545) tokenizer: physical lines, unfolding, content lines,
parameters, TEXT escaping and the BEGIN/END component tree.

The tree remembers the physical line span of every component and property so
the writer can edit a file by line surgery and leave everything else
byte-identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone


class CalendarError(Exception):
    """An expected, user-actionable failure. Tool wrappers show its message."""


class ICalError(CalendarError):
    """The iCalendar text is malformed."""


_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+")


def split_physical(text: str) -> list[str]:
    """Split on CRLF, LF or lone CR, keeping line endings.

    str.splitlines() is not usable: it also splits on form feed, vertical
    tab, U+2028 and friends, all of which may legally appear inside values.
    """
    return _LINE_RE.findall(text)


def line_body(phys: str) -> str:
    """A physical line without its line ending."""
    return phys.rstrip("\r\n")


@dataclass
class Prop:
    name: str
    params: dict[str, list[str]]
    value: str
    first: int  # first physical line index
    last: int  # last physical line index (continuation lines included)

    def param(self, key: str) -> str | None:
        vals = self.params.get(key.upper())
        return vals[0] if vals else None


@dataclass
class Component:
    name: str
    first: int  # physical index of BEGIN line
    last: int = -1  # physical index of END line
    props: list[Prop] = field(default_factory=list)
    children: list[Component] = field(default_factory=list)

    def get(self, name: str) -> Prop | None:
        name = name.upper()
        for p in self.props:
            if p.name == name:
                return p
        return None

    def get_all(self, name: str) -> list[Prop]:
        name = name.upper()
        return [p for p in self.props if p.name == name]

    def sub(self, name: str) -> list[Component]:
        name = name.upper()
        return [c for c in self.children if c.name == name]


@dataclass
class Parsed:
    """A parsed file: top-level components (normally one VCALENDAR) and the
    non-fatal problems met on the way (malformed lines that were skipped)."""

    roots: list[Component]
    lines: list[str]  # physical lines with their endings
    warnings: list[str]


def _parse_content_line(s: str) -> tuple[str, dict[str, list[str]], str]:
    n = len(s)
    i = 0
    while i < n and s[i] not in ";:":
        i += 1
    if i >= n:
        raise ValueError("no ':' separating the property name from its value")
    name = s[:i].strip().upper()
    if not name:
        raise ValueError("empty property name")
    if "." in name:  # RFC 5545 allows an (ignored) group prefix, "item1.X-FOO"
        name = name.rsplit(".", 1)[1]
    params: dict[str, list[str]] = {}
    while s[i] == ";":
        i += 1
        j = i
        while j < n and s[j] not in "=:;":
            j += 1
        if j >= n or s[j] != "=":
            raise ValueError("parameter without '='")
        pname = s[i:j].strip().upper()
        i = j + 1
        values: list[str] = []
        while True:
            if i < n and s[i] == '"':
                k = s.find('"', i + 1)
                if k < 0:
                    raise ValueError("unterminated quoted parameter value")
                values.append(s[i + 1 : k])
                i = k + 1
            else:
                k = i
                while k < n and s[k] not in ",;:":
                    k += 1
                values.append(s[i:k])
                i = k
            if i < n and s[i] == ",":
                i += 1
                continue
            break
        params.setdefault(pname, []).extend(values)
        if i >= n:
            raise ValueError("no ':' after parameters")
        if s[i] not in ";:":
            raise ValueError("unexpected character after parameter value")
    return name, params, s[i + 1 :]


def parse(text: str) -> Parsed:
    """Parse iCalendar text. Raises ICalError for structural damage (no
    VCALENDAR, unbalanced BEGIN/END). Malformed single lines are skipped and
    reported in Parsed.warnings."""
    if text.startswith("﻿"):
        text = text[1:]
    phys = split_physical(text)
    warnings: list[str] = []
    roots: list[Component] = []
    stack: list[Component] = []

    i = 0
    total = len(phys)
    while i < total:
        body = line_body(phys[i])
        first = i
        i += 1
        if body == "":
            continue
        if body[0] in " \t":
            warnings.append(
                f"line {first + 1}: continuation line with nothing to continue; skipped"
            )
            continue
        parts = [body]
        while i < total:
            nxt = line_body(phys[i])
            if nxt and nxt[0] in " \t":
                parts.append(nxt[1:])
                i += 1
            else:
                break
        last = i - 1
        logical = "".join(parts)
        try:
            name, params, value = _parse_content_line(logical)
        except ValueError as e:
            warnings.append(f"line {first + 1}: malformed property ({e}); skipped")
            continue
        if name == "BEGIN":
            comp = Component(value.strip().upper(), first)
            if stack:
                stack[-1].children.append(comp)
            else:
                roots.append(comp)
            stack.append(comp)
        elif name == "END":
            endname = value.strip().upper()
            if not stack:
                raise ICalError(f"line {first + 1}: END:{endname} without a matching BEGIN")
            if stack[-1].name != endname:
                raise ICalError(
                    f"line {first + 1}: END:{endname} does not match "
                    f"BEGIN:{stack[-1].name} (line {stack[-1].first + 1})"
                )
            stack[-1].last = last
            stack.pop()
        else:
            if not stack:
                warnings.append(f"line {first + 1}: property {name} outside any component; skipped")
                continue
            stack[-1].props.append(Prop(name, params, value, first, last))
    if stack:
        raise ICalError(
            f"BEGIN:{stack[-1].name} on line {stack[-1].first + 1} is never closed "
            f"(file is truncated or damaged)"
        )
    if not any(r.name == "VCALENDAR" for r in roots):
        raise ICalError("no BEGIN:VCALENDAR found - this does not look like an iCalendar file")
    return Parsed(roots, phys, warnings)


# --- value helpers --------------------------------------------------------


def unescape_text(v: str) -> str:
    out: list[str] = []
    i = 0
    n = len(v)
    while i < n:
        c = v[i]
        if c == "\\" and i + 1 < n:
            d = v[i + 1]
            if d in "nN":
                out.append("\n")
            else:
                out.append(d)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def escape_text(v: str) -> str:
    return (
        v.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def split_text_list(v: str) -> list[str]:
    """Split a TEXT list on unescaped commas (CATEGORIES, RESOURCES)."""
    out: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            cur.append(v[i : i + 2])
            i += 2
        elif c == ",":
            out.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    out.append("".join(cur))
    return [unescape_text(x) for x in out]


_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})([Zz]?)$")


def is_date_value(v: str) -> bool:
    return bool(_DATE_RE.match(v.strip()))


def parse_date(v: str) -> date:
    m = _DATE_RE.match(v.strip())
    if not m:
        raise ICalError(f"invalid DATE value {v!r} (expected YYYYMMDD)")
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError as e:
        raise ICalError(f"invalid DATE value {v!r}: {e}") from e


def parse_datetime(v: str) -> tuple[datetime, bool]:
    """Returns (naive wall-clock datetime, is_utc)."""
    m = _DT_RE.match(v.strip())
    if not m:
        raise ICalError(f"invalid DATE-TIME value {v!r} (expected YYYYMMDDTHHMMSS[Z])")
    h, mi, s = int(m[4]), int(m[5]), int(m[6])
    try:
        if s == 60:  # leap second: clamp, nothing here is second-accurate
            s = 59
        return datetime(int(m[1]), int(m[2]), int(m[3]), h, mi, s), bool(m[7])
    except ValueError as e:
        raise ICalError(f"invalid DATE-TIME value {v!r}: {e}") from e


_DUR_RE = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


@dataclass(frozen=True)
class Duration:
    sign: int
    days: int  # includes weeks * 7; nominal (calendar) days
    seconds: int  # exact part

    def nominal(self) -> timedelta:
        return self.sign * timedelta(days=self.days, seconds=self.seconds)


def parse_duration(v: str) -> Duration:
    s = v.strip().upper()
    m = _DUR_RE.match(s)
    if not m or s in ("P", "+P", "-P") or s.endswith("T"):
        raise ICalError(f"invalid DURATION value {v!r} (expected e.g. PT1H30M, P1D, P2W)")
    sign = -1 if m[1] == "-" else 1
    weeks, days, hrs, mins, secs = (int(x) if x else 0 for x in m.groups()[1:])
    return Duration(sign, weeks * 7 + days, hrs * 3600 + mins * 60 + secs)


_OFFSET_RE = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?$")


def parse_utc_offset(v: str) -> timedelta:
    m = _OFFSET_RE.match(v.strip())
    if not m:
        raise ICalError(f"invalid UTC offset {v!r} (expected +HHMM or -HHMM)")
    td = timedelta(hours=int(m[2]), minutes=int(m[3]), seconds=int(m[4] or 0))
    return -td if m[1] == "-" else td


def fold_line(line: str, limit: int = 75) -> list[str]:
    """Fold a logical line into physical lines of at most `limit` octets
    (UTF-8), never splitting a character. Continuations start with a space."""
    out: list[str] = []
    cur = ""
    cur_bytes = 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if cur_bytes + b > limit:
            out.append(cur)
            cur = " "
            cur_bytes = 1
        cur += ch
        cur_bytes += b
    out.append(cur)
    return out


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
