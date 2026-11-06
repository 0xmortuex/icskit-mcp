"""RFC 5545 recurrence rules (RRULE), implemented from the RFC.

The engine works purely on wall-clock values: naive `datetime` (timed events)
or `date` (all-day events). Mapping wall-clock values to instants in a time
zone is the caller's job - that separation is what keeps recurrences stable
across daylight saving changes ("every Monday at 09:00" stays at 09:00).

Supported: FREQ=DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY
(with ordinals such as 2MO, -1FR where the RFC allows them), BYMONTHDAY
(negative values count from the month end), BYMONTH, BYSETPOS, WKST.
Anything else (HOURLY and finer, BYWEEKNO, BYYEARDAY, BYHOUR...) raises
ICalError instead of being ignored, so a rule is never silently mis-expanded.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .ical import ICalError, is_date_value, parse_date, parse_datetime

Wall = date | datetime  # datetime here is always naive

_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_UNSUPPORTED_FREQS = ("SECONDLY", "MINUTELY", "HOURLY")
_UNSUPPORTED_PARTS = ("BYWEEKNO", "BYYEARDAY", "BYHOUR", "BYMINUTE", "BYSECOND", "RSCALE", "SKIP")


@dataclass(frozen=True)
class Rule:
    freq: str
    interval: int = 1
    count: int | None = None
    until: Wall | None = None  # naive wall value as written in the rule
    until_utc: bool = False  # UNTIL carried a trailing Z
    bymonth: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()
    byday: tuple[tuple[int, int], ...] = ()  # (ordinal or 0, weekday 0=Mon)
    bysetpos: tuple[int, ...] = ()
    wkst: int = 0


def _ints(name: str, v: str, lo: int, hi: int, allow_zero: bool = False) -> tuple[int, ...]:
    out: list[int] = []
    for part in v.split(","):
        try:
            n = int(part)
        except ValueError:
            raise ICalError(f"RRULE {name}={v!r}: {part!r} is not an integer") from None
        if (n == 0 and not allow_zero) or not (lo <= n <= hi):
            raise ICalError(f"RRULE {name}={v!r}: {n} is out of range")
        out.append(n)
    return tuple(out)


def parse_rule(text: str) -> Rule:
    text = text.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    parts: dict[str, str] = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ICalError(f"RRULE part {chunk!r} has no '='")
        k, v = chunk.split("=", 1)
        k = k.strip().upper()
        if k in parts:
            raise ICalError(f"RRULE repeats {k}")
        parts[k] = v.strip()
    freq = parts.pop("FREQ", "").upper()
    if not freq:
        raise ICalError("RRULE has no FREQ")
    if freq in _UNSUPPORTED_FREQS:
        raise ICalError(f"RRULE FREQ={freq} is not supported (only DAILY, WEEKLY, MONTHLY, YEARLY)")
    if freq not in _FREQS:
        raise ICalError(f"RRULE FREQ={freq!r} is not a valid frequency")
    for bad in _UNSUPPORTED_PARTS:
        if bad in parts:
            raise ICalError(f"RRULE part {bad} is not supported")

    interval = 1
    if "INTERVAL" in parts:
        interval = _ints("INTERVAL", parts.pop("INTERVAL"), 1, 10**6)[0]
    count: int | None = None
    if "COUNT" in parts:
        count = _ints("COUNT", parts.pop("COUNT"), 1, 10**7)[0]
    until: Wall | None = None
    until_utc = False
    if "UNTIL" in parts:
        raw = parts.pop("UNTIL")
        if is_date_value(raw):
            until = parse_date(raw)
        else:
            until, until_utc = parse_datetime(raw)
    bymonth = _ints("BYMONTH", parts.pop("BYMONTH"), 1, 12) if "BYMONTH" in parts else ()
    bymonthday = (
        _ints("BYMONTHDAY", parts.pop("BYMONTHDAY"), -31, 31) if "BYMONTHDAY" in parts else ()
    )
    bysetpos = _ints("BYSETPOS", parts.pop("BYSETPOS"), -366, 366) if "BYSETPOS" in parts else ()
    wkst = 0
    if "WKST" in parts:
        w = parts.pop("WKST").upper()
        if w not in _DAYS:
            raise ICalError(f"RRULE WKST={w!r} is not a weekday (MO..SU)")
        wkst = _DAYS[w]
    byday: list[tuple[int, int]] = []
    if "BYDAY" in parts:
        for item in parts.pop("BYDAY").upper().split(","):
            item = item.strip()
            if len(item) < 2 or item[-2:] not in _DAYS:
                raise ICalError(f"RRULE BYDAY item {item!r} is not like MO, 2TU or -1FR")
            ordinal_text = item[:-2]
            ordinal = 0
            if ordinal_text:
                try:
                    ordinal = int(ordinal_text)
                except ValueError:
                    raise ICalError(f"RRULE BYDAY item {item!r} has a bad ordinal") from None
                if ordinal == 0 or abs(ordinal) > 53:
                    raise ICalError(f"RRULE BYDAY item {item!r} has an out-of-range ordinal")
            byday.append((ordinal, _DAYS[item[-2:]]))
    if parts:
        raise ICalError(f"RRULE has unknown part(s): {', '.join(sorted(parts))}")
    if any(n for n, _ in byday) and freq not in ("MONTHLY", "YEARLY"):
        raise ICalError(f"RRULE BYDAY ordinals (like 2MO) are only valid with MONTHLY or YEARLY, not {freq}")
    if bysetpos and not (byday or bymonthday or bymonth):
        raise ICalError("RRULE BYSETPOS needs another BYxxx part to select from")
    return Rule(freq, interval, count, until, until_utc, bymonth, bymonthday, tuple(byday), bysetpos, wkst)


def _month_days(y: int, m: int) -> list[date]:
    n = calendar.monthrange(y, m)[1]
    return [date(y, m, d) for d in range(1, n + 1)]


def _nth(days: list[date], weekday: int, n: int) -> date | None:
    matches = [d for d in days if d.weekday() == weekday]
    try:
        return matches[n - 1] if n > 0 else matches[n]
    except IndexError:
        return None


def _period_days(rule: Rule, anchor: date, k: int) -> list[date] | None:
    """The dates covered by period number k, or None past the supported range."""
    iv = rule.interval
    try:
        if rule.freq == "DAILY":
            return [anchor + timedelta(days=iv * k)]
        if rule.freq == "WEEKLY":
            ws = anchor - timedelta(days=(anchor.weekday() - rule.wkst) % 7)
            start = ws + timedelta(days=7 * iv * k)
            return [start + timedelta(days=i) for i in range(7)]
        if rule.freq == "MONTHLY":
            m0 = anchor.year * 12 + anchor.month - 1 + iv * k
            y, m = divmod(m0, 12)
            if y > 9999:
                return None
            return _month_days(y, m + 1)
        y = anchor.year + iv * k
        if y > 9999:
            return None
        out: list[date] = []
        for m in range(1, 13):
            out.extend(_month_days(y, m))
        return out
    except OverflowError:
        return None


def _first_period(rule: Rule, anchor: date, lo: date) -> int:
    """A period index safely at or before the one containing `lo`."""
    iv = rule.interval
    if rule.freq == "DAILY":
        k = (lo - anchor).days // iv
    elif rule.freq == "WEEKLY":
        ws = anchor - timedelta(days=(anchor.weekday() - rule.wkst) % 7)
        k = (lo - ws).days // (7 * iv)
    elif rule.freq == "MONTHLY":
        k = ((lo.year * 12 + lo.month - 1) - (anchor.year * 12 + anchor.month - 1)) // iv
    else:
        k = (lo.year - anchor.year) // iv
    return max(0, k - 1)


def _select(rule: Rule, anchor: date, days: list[date]) -> list[date]:
    bymonth = set(rule.bymonth)
    bymonthday = set(rule.bymonthday)
    byday = rule.byday
    if rule.freq == "YEARLY" and not byday and not bymonthday:
        bymonthday = {anchor.day}
        if not bymonth:
            bymonth = {anchor.month}
    elif rule.freq == "MONTHLY" and not byday and not bymonthday:
        bymonthday = {anchor.day}
    elif rule.freq == "WEEKLY" and not byday:
        byday = ((0, anchor.weekday()),)

    plain = {wd for n, wd in byday if n == 0}
    ordered = [(n, wd) for n, wd in byday if n != 0]
    ordinal_dates: set[date] = set()
    if ordered:
        by_month = rule.freq == "MONTHLY" or bool(bymonth)
        groups: dict[int, list[date]] = {}
        for d in days:
            groups.setdefault(d.month if by_month else 0, []).append(d)
        for scope in groups.values():
            for n, wd in ordered:
                hit = _nth(scope, wd, n)
                if hit is not None:
                    ordinal_dates.add(hit)

    out: list[date] = []
    for d in days:
        if bymonth and d.month not in bymonth:
            continue
        if bymonthday:
            dim = calendar.monthrange(d.year, d.month)[1]
            if d.day not in bymonthday and (d.day - dim - 1) not in bymonthday:
                continue
        if byday and d.weekday() not in plain and d not in ordinal_dates:
            continue
        out.append(d)
    return out


def _as_datetime(w: Wall) -> datetime:
    if isinstance(w, datetime):
        return w
    return datetime.combine(w, time.min)


def iterate(
    rule: Rule,
    dtstart: Wall,
    lo: Wall | None = None,
    hi: Wall | None = None,
    until: Wall | None = None,
) -> Iterator[Wall]:
    """Yield the occurrences of the rule in increasing order.

    dtstart is the first instance (always yielded, even if it does not match
    the rule, as RFC 5545 defines). `lo`/`hi` bound the yielded values
    (inclusive); iteration stops after `hi`. `until` is the already converted
    wall-clock UNTIL; the rule's own raw UNTIL is not consulted here.
    COUNT is honoured exactly, which means rules with COUNT are always
    walked from the start; rules without COUNT jump straight to `lo`.
    """
    is_dt = isinstance(dtstart, datetime)
    anchor = dtstart.date() if isinstance(dtstart, datetime) else dtstart
    tod = dtstart.time() if isinstance(dtstart, datetime) else None
    lo_dt = _as_datetime(lo) if lo is not None else None
    hi_dt = _as_datetime(hi) if hi is not None else None
    until_dt: datetime | None = None
    if until is not None:
        until_dt = datetime.combine(until, time.max) if not isinstance(until, datetime) else until

    k = 0
    if rule.count is None and lo_dt is not None:
        k = _first_period(rule, anchor, lo_dt.date())
    emitted = 0
    # Skipped periods only exist when there is no COUNT, so `emitted` is exact.
    while True:
        days = _period_days(rule, anchor, k)
        if days is None:
            return
        if hi_dt is not None and days[0] > hi_dt.date():
            return
        cand = _select(rule, anchor, days)
        if k == 0 and anchor not in cand:
            cand.append(anchor)
            cand.sort()
        if rule.bysetpos:
            picked: set[date] = set()
            for pos in rule.bysetpos:
                idx = pos - 1 if pos > 0 else pos
                if -len(cand) <= idx < len(cand):
                    picked.add(cand[idx])
            if k == 0 and anchor not in picked:
                picked.add(anchor)
            cand = sorted(picked)
        for d in cand:
            if d < anchor:
                continue
            value: Wall = datetime.combine(d, tod) if is_dt and tod is not None else d
            vdt = _as_datetime(value)
            if until_dt is not None and vdt > until_dt:
                return
            if hi_dt is not None and vdt > hi_dt:
                return
            emitted += 1
            if rule.count is not None and emitted > rule.count:
                return
            if lo_dt is None or vdt >= lo_dt:
                yield value
            if rule.count is not None and emitted == rule.count:
                return
        k += 1
