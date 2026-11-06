"""A 10,000-event calendar must load and answer queries in seconds, with
numbers checked against an independent count."""

import random
import time
from datetime import date, timedelta

import pytest
from conftest import write

from icskit_mcp import server

N = 10_000


def build_big_calendar(path):
    rng = random.Random(10_000)
    zones = ["Europe/Istanbul", "Europe/Berlin", "America/New_York"]
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//perf//EN"]
    base = date(2025, 1, 1)
    singles_in_march = 0
    for i in range(N):
        kind = i % 20
        d = base + timedelta(days=rng.randint(0, 3 * 365))
        z = rng.choice(zones)
        h = rng.randint(7, 18)
        out += ["BEGIN:VEVENT", f"UID:perf-{i}@test", "DTSTAMP:20250101T000000Z",
                f"SUMMARY:Event number {i} {'alpha' if i % 7 == 0 else 'beta'}"]
        if kind < 12:  # single timed events
            out += [f"DTSTART;TZID={z}:{d:%Y%m%d}T{h:02d}0000", f"DTEND;TZID={z}:{d:%Y%m%d}T{h:02d}4500"]
            if date(2026, 3, 1) <= d <= date(2026, 3, 31):
                singles_in_march += 1
        elif kind < 14:  # all-day
            out += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        elif kind < 18:  # weekly, bounded
            out += [f"DTSTART;TZID={z}:{d:%Y%m%d}T{h:02d}0000", "DURATION:PT30M",
                    f"RRULE:FREQ=WEEKLY;COUNT={rng.randint(5, 120)}"]
        elif kind < 19:  # monthly nth weekday, endless
            out += [f"DTSTART;TZID={z}:{d:%Y%m%d}T{h:02d}0000", "DURATION:PT1H",
                    f"RRULE:FREQ=MONTHLY;BYDAY={rng.choice([1, 2, -1])}{rng.choice(['MO', 'TU', 'FR'])}"]
        else:  # daily weekdays, endless, with an exception
            out += [f"DTSTART;TZID={z}:{d:%Y%m%d}T{h:02d}1500", "DURATION:PT15M",
                    "RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR",
                    f"EXDATE;TZID={z}:{(d + timedelta(days=7)):%Y%m%d}T{h:02d}1500"]
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    path.write_bytes(("\r\n".join(out) + "\r\n").encode("utf-8"))
    return singles_in_march


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    d = tmp_path_factory.mktemp("big")
    singles = build_big_calendar(d / "big.ics")
    write(d / "small.ics", "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
    t0 = time.perf_counter()
    lib = server.configure(d)
    lib.refresh()
    load = time.perf_counter() - t0
    yield d, singles, load
    server._lib = None


@pytest.mark.slow
def test_load_time_and_counts(big):
    d, _, load = big
    f = server.lib().get_file("big.ics")
    assert len(f.series) == N and not f.warnings
    assert load < 30, f"loading 10k events took {load:.1f}s"


@pytest.mark.slow
def test_month_queries_are_fast_and_consistent(big):
    d, singles, _ = big
    t0 = time.perf_counter()
    out = server.agenda("2026-03-01", "2026-03-31", "Europe/Berlin", limit=100000)
    t_agenda = time.perf_counter() - t0
    n = int(out.split("occurrence(s)")[0].split()[-1])
    assert n >= singles  # singles alone, plus recurring instances
    assert t_agenda < 15, f"agenda over a month took {t_agenda:.1f}s"

    t0 = time.perf_counter()
    server.find_free_time("2026-03-02", "2026-03-06", 30, "Europe/Berlin")
    server.find_conflicts("2026-03-01", "2026-03-31", "Europe/Berlin")
    server.search_events(query="alpha", start="2026-03-01", end="2026-03-31", tz="UTC")
    server.next_occurrences("perf-18@test", 10, "2026-03-01", "UTC")
    assert time.perf_counter() - t0 < 40


@pytest.mark.slow
def test_second_call_does_not_reparse_the_file(big):
    d, _, _ = big
    lib = server.lib()
    before = lib.get_file("big.ics")
    server.calendar_overview()
    assert lib.get_file("big.ics") is before  # unchanged on disk -> same parsed object
