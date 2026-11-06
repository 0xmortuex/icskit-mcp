"""Free-time and conflict answers must be exact, not approximately right."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import vcal, write

from icskit_mcp import server
from icskit_mcp.ical import CalendarError
from icskit_mcp.timeutil import (
    merge,
    parse_days,
    parse_working_hours,
    subtract,
)

UTC = timezone.utc


def vevent(uid, start, end, summary="E", *extra):
    return ["BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20260101T000000Z", f"DTSTART:{start}", f"DTEND:{end}",
            f"SUMMARY:{summary}", *extra, "END:VEVENT"]


@pytest.fixture
def two_cals(tmp_path):
    write(tmp_path / "a.ics", vcal(
        *vevent("a1", "20260303T093000Z", "20260303T100000Z"),
        *vevent("a2", "20260303T110000Z", "20260303T123000Z"),
        *vevent("a3", "20260303T123000Z", "20260303T130000Z"),  # back to back with a2
    ))
    write(tmp_path / "b.ics", vcal(*vevent("b1", "20260303T150000Z", "20260303T160000Z")))
    server.configure(tmp_path)
    yield tmp_path
    server._lib = None


def slots(text):
    """Parse 'HH:MM-HH:MM' slot lines out of the tool output."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) > 11 and ln[2] == ":" and ln[5] == "-" and ln[8] == ":":
            out.append(ln[:11])
    return out


def free(**kw):
    args = dict(start="2026-03-03", end="2026-03-03", duration_minutes=30, tz="UTC", days="Tue")
    args.update(kw)
    return server.find_free_time(**args)


def test_exact_free_blocks_across_two_calendars(two_cals):
    assert slots(free()) == ["09:00-09:30", "10:00-11:00", "13:00-15:00", "16:00-17:00"]
    # a block of exactly 60 minutes qualifies for a 60 minute meeting
    assert slots(free(duration_minutes=60)) == ["10:00-11:00", "13:00-15:00", "16:00-17:00"]
    assert slots(free(duration_minutes=61)) == ["13:00-15:00"]
    assert slots(free(duration_minutes=121)) == []


def test_buffers_pad_both_sides_of_every_busy_event(two_cals):
    out = free(buffer_minutes=15, duration_minutes=30)
    assert slots(out) == ["10:15-10:45", "13:15-14:45", "16:15-17:00"]
    assert slots(free(buffer_minutes=15, duration_minutes=15)) == [
        "09:00-09:15", "10:15-10:45", "13:15-14:45", "16:15-17:00"
    ]
    assert "15 min buffer" in out


def test_calendar_filter_only_counts_the_chosen_calendars(two_cals):
    assert slots(free(calendars=["b"], duration_minutes=60)) == ["09:00-15:00", "16:00-17:00"]
    assert slots(free(calendars=["a.ics"], duration_minutes=60)) == ["10:00-11:00", "13:00-17:00"]


def test_split_working_hours_and_window_clipping(two_cals):
    assert slots(free(working_hours="09:00-12:00,13:00-17:00", duration_minutes=60)) == [
        "10:00-11:00", "13:00-15:00", "16:00-17:00"
    ]
    out = free(start="2026-03-03T10:30", end="2026-03-03T14:00", duration_minutes=30)
    assert slots(out) == ["10:30-11:00", "13:00-14:00"]


def test_days_filter_and_multi_day_range(two_cals):
    out = free(start="2026-03-02", end="2026-03-04", days="Mon-Wed", duration_minutes=600)
    assert "No free slot" in out
    out = free(start="2026-03-02", end="2026-03-04", days="Mon,Wed", duration_minutes=60)
    assert slots(out) == ["09:00-17:00", "09:00-17:00"]
    assert "Total free in these slots: 16h" in out


def test_transparent_all_day_and_cancelled_do_not_block_by_default(tmp_path):
    write(tmp_path / "c.ics", vcal(
        *vevent("t1", "20260303T090000Z", "20260303T100000Z", "Free", "TRANSP:TRANSPARENT"),
        *vevent("t2", "20260303T100000Z", "20260303T110000Z", "Gone", "STATUS:CANCELLED"),
        "BEGIN:VEVENT", "UID:ad", "DTSTAMP:20260101T000000Z", "DTSTART;VALUE=DATE:20260303",
        "DTEND;VALUE=DATE:20260304", "SUMMARY:OOO", "END:VEVENT",
    ))
    server.configure(tmp_path)
    try:
        assert slots(free(duration_minutes=480)) == ["09:00-17:00"]
        assert slots(free(duration_minutes=60, include_transparent=True)) == ["10:00-17:00"]
        assert slots(free(duration_minutes=60, include_all_day=True)) == []
    finally:
        server._lib = None


def test_dst_days_keep_their_wall_hours_and_real_lengths(tmp_path):
    write(tmp_path / "d.ics", vcal(*vevent("x", "20260101T000000Z", "20260101T010000Z")))
    server.configure(tmp_path)
    try:
        # 9-to-5 is 8 real hours on the spring-forward Sunday, and the day itself is 23h long
        out = server.find_free_time("2026-03-08", "2026-03-08", 60, "America/New_York", "09:00-17:00", "all")
        assert slots(out) == ["09:00-17:00"] and "(8h)" in out
        out = server.find_free_time("2026-03-08", "2026-03-08", 60, "America/New_York", "00:00-24:00", "all")
        assert "Total free in these slots: 23h" in out
        out = server.find_free_time("2026-11-01", "2026-11-01", 60, "America/New_York", "00:00-24:00", "all")
        assert "Total free in these slots: 25h" in out
        out = server.find_free_time("2026-03-29", "2026-03-29", 60, "Europe/Berlin", "00:00-24:00", "all")
        assert "Total free in these slots: 23h" in out
        out = server.find_free_time("2026-03-29", "2026-03-29", 60, "Europe/Istanbul", "00:00-24:00", "all")
        assert "Total free in these slots: 1d" not in out and "24h" in out
    finally:
        server._lib = None


def test_free_time_respects_the_zone_of_the_question(two_cals):
    # the UTC events are 12:30-13:00, 14:00-16:00 and 18:00-19:00 in Istanbul (+03)
    out = server.find_free_time("2026-03-03", "2026-03-03", 30, "Europe/Istanbul", "09:00-17:00", "Tue")
    assert "zone Europe/Istanbul" in out and "+03" in out
    assert slots(out) == ["09:00-12:30", "13:00-14:00", "16:00-17:00"]


def test_free_time_argument_errors(two_cals):
    with pytest.raises(CalendarError, match="positive"):
        free(duration_minutes=0)
    with pytest.raises(CalendarError, match="working_hours"):
        free(working_hours="9-5")
    with pytest.raises(CalendarError, match="ends before"):
        free(working_hours="17:00-09:00")
    with pytest.raises(CalendarError, match="days"):
        free(days="Funday")
    with pytest.raises(CalendarError, match="after start"):
        free(start="2026-03-04", end="2026-03-02")
    with pytest.raises(CalendarError, match="IANA"):
        free(tz="CET-ish")
    with pytest.raises(CalendarError, match="no calendar matches"):
        free(calendars=["nope"])


# --- conflicts -----------------------------------------------------------------


def test_conflicts_across_calendars_and_touching_is_not_a_conflict(tmp_path):
    write(tmp_path / "a.ics", vcal(
        *vevent("a1", "20260303T090000Z", "20260303T100000Z", "Standup"),
        *vevent("a2", "20260303T100000Z", "20260303T110000Z", "Next"),  # touches a1
    ))
    write(tmp_path / "b.ics", vcal(
        *vevent("b1", "20260303T093000Z", "20260303T103000Z", "Dentist"),  # overlaps both
    ))
    server.configure(tmp_path)
    try:
        out = server.find_conflicts("2026-03-03", "2026-03-03")
        assert "2 conflicting pair(s)" in out
        assert "overlap 2026-03-03 09:30 UTC -> 10:00 (30m) [different calendars]" in out
        assert "overlap 2026-03-03 10:00 UTC -> 10:30 (30m) [different calendars]" in out
        assert "Standup" in out and "Dentist" in out
    finally:
        server._lib = None


def test_conflict_options_for_transparent_all_day_cancelled(tmp_path):
    write(tmp_path / "a.ics", vcal(
        *vevent("a1", "20260303T090000Z", "20260303T100000Z", "Real"),
        *vevent("a2", "20260303T093000Z", "20260303T103000Z", "Maybe", "TRANSP:TRANSPARENT"),
        *vevent("a3", "20260303T093000Z", "20260303T103000Z", "Dead", "STATUS:CANCELLED"),
        "BEGIN:VEVENT", "UID:ad", "DTSTAMP:20260101T000000Z", "DTSTART;VALUE=DATE:20260303",
        "DTEND;VALUE=DATE:20260304", "SUMMARY:Holiday", "END:VEVENT",
    ))
    server.configure(tmp_path)
    try:
        assert "No conflicts" in server.find_conflicts("2026-03-03", "2026-03-03")
        out = server.find_conflicts("2026-03-03", "2026-03-03", include_transparent=True)
        assert "1 conflicting pair(s)" in out and "Maybe" in out and "Dead" not in out
        out = server.find_conflicts("2026-03-03", "2026-03-03", include_transparent=True, include_all_day=True)
        assert "Holiday" in out
        assert "Dead" not in out
    finally:
        server._lib = None


def test_conflicts_inside_recurring_series_and_dst(tmp_path):
    # two weekly meetings that overlap only after the clocks change
    write(tmp_path / "a.ics", vcal(
        "BEGIN:VEVENT", "UID:r1", "DTSTAMP:20260101T000000Z", "DTSTART;TZID=Europe/Berlin:20260323T100000",
        "DTEND;TZID=Europe/Berlin:20260323T110000", "RRULE:FREQ=WEEKLY;COUNT=2", "SUMMARY:Berlin sync", "END:VEVENT",
        "BEGIN:VEVENT", "UID:r2", "DTSTAMP:20260101T000000Z", "DTSTART:20260323T090000Z", "DTEND:20260323T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=2", "SUMMARY:UTC sync", "END:VEVENT",
    ))
    server.configure(tmp_path)
    try:
        # week 1: Berlin 10:00 CET = 09:00Z -> identical times -> conflict
        # week 2 (Mar 30, CEST): Berlin 10:00 = 08:00Z, UTC sync 09:00Z -> no overlap
        out = server.find_conflicts("2026-03-23", "2026-04-05", "UTC")
        assert "1 conflicting pair(s)" in out and "2026-03-23" in out and "2026-03-30" not in out.split("A:")[0]
    finally:
        server._lib = None


# --- interval primitives ------------------------------------------------------------


def iv(a, b):
    return (datetime(2026, 1, 1, a, tzinfo=UTC), datetime(2026, 1, 1, b, tzinfo=UTC))


def test_merge_and_subtract_primitives():
    assert merge([iv(9, 11), iv(10, 12), iv(12, 13), iv(15, 16)]) == [iv(9, 13), iv(15, 16)]
    assert subtract([iv(9, 17)], [iv(10, 11), iv(12, 13)]) == [iv(9, 10), iv(11, 12), iv(13, 17)]
    assert subtract([iv(9, 17)], [iv(8, 10), iv(16, 18)]) == [iv(10, 16)]
    assert subtract([iv(9, 10), iv(11, 12)], [iv(9, 12)]) == []
    assert subtract([iv(9, 10)], []) == [iv(9, 10)]
    assert subtract([iv(9, 17)], [iv(9, 17)]) == []


def test_parsers():
    assert parse_days("Mon-Fri") == {0, 1, 2, 3, 4}
    assert parse_days("Fri-Mon") == {4, 5, 6, 0}
    assert parse_days("all") == set(range(7))
    assert parse_working_hours("08:30-12:00, 13:00-24:00")[1][1] is None
    assert timedelta(minutes=30) == timedelta(seconds=1800)
