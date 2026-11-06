"""Event expansion with real files: DST gap/overlap, exact lengths, overrides,
exceptions, cancelled events, floating and all-day times."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import vcal, write

from icskit_mcp.library import Library
from icskit_mcp.tz import require_zone

UTC = timezone.utc


def occ_utc(tmp_path, body, start, end, tz="UTC", include_cancelled=False):
    """Expanded occurrences of the events in `body` as (start_utc, end_utc, summary)."""
    write(tmp_path / "t.ics", vcal(*body))
    lib = Library(tmp_path)
    z = require_zone(tz)
    ws = datetime.fromisoformat(start).replace(tzinfo=z)
    we = datetime.fromisoformat(end).replace(tzinfo=z)
    out = []
    for o in lib.occurrences(ws, we, z, include_cancelled=include_cancelled):
        s, e = o.span(z)
        out.append((s, e, o.event.summary))
    assert not lib.all_warnings(), lib.all_warnings()
    return out


def ev(*lines):
    return ["BEGIN:VEVENT", "UID:u1", "DTSTAMP:20260101T000000Z", *lines, "END:VEVENT"]


def u(*a):
    return datetime(*a, tzinfo=UTC)


def test_weekly_berlin_meeting_keeps_wall_time_across_spring_forward(tmp_path):
    body = ev("DTSTART;TZID=Europe/Berlin:20260320T090000", "DURATION:PT1H",
              "RRULE:FREQ=WEEKLY;COUNT=4", "SUMMARY:Sync")
    got = occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-05-01T00:00")
    assert [s for s, _, _ in got] == [
        u(2026, 3, 20, 8), u(2026, 3, 27, 8),  # CET, +01:00
        u(2026, 4, 3, 7), u(2026, 4, 10, 7),  # CEST, +02:00 (clocks changed on Mar 29)
    ]
    assert all(e - s == timedelta(hours=1) for s, e, _ in got)


def test_new_york_event_in_the_spring_forward_gap_moves_to_the_post_gap_hour(tmp_path):
    body = ev("DTSTART;TZID=America/New_York:20260301T023000", "DTEND;TZID=America/New_York:20260301T033000",
              "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Gap")
    got = occ_utc(tmp_path, body, "2026-02-27T00:00", "2026-03-31T00:00")
    assert [s for s, _, _ in got] == [
        u(2026, 3, 1, 7, 30),  # 02:30 EST
        u(2026, 3, 8, 7, 30),  # 02:30 does not exist: read with EST -> 03:30 EDT
        u(2026, 3, 15, 6, 30),  # 02:30 EDT
    ]


def test_new_york_event_in_the_fall_back_overlap_uses_the_first_occurrence(tmp_path):
    body = ev("DTSTART;TZID=America/New_York:20261025T013000", "DURATION:PT30M",
              "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Overlap")
    got = occ_utc(tmp_path, body, "2026-10-20T00:00", "2026-11-30T00:00")
    assert [s for s, _, _ in got] == [
        u(2026, 10, 25, 5, 30),  # EDT
        u(2026, 11, 1, 5, 30),  # 01:30 happens twice; the first (EDT) is meant
        u(2026, 11, 8, 6, 30),  # EST
    ]


def test_berlin_fall_back_and_spring_forward_gap(tmp_path):
    body = ev("DTSTART;TZID=Europe/Berlin:20261024T023000", "DURATION:PT1H",
              "RRULE:FREQ=DAILY;COUNT=3", "SUMMARY:Night")
    got = occ_utc(tmp_path, body, "2026-10-20T00:00", "2026-10-30T00:00")
    assert [s for s, _, _ in got] == [u(2026, 10, 24, 0, 30), u(2026, 10, 25, 0, 30), u(2026, 10, 26, 1, 30)]
    body = ev("DTSTART;TZID=Europe/Berlin:20260328T023000", "DURATION:PT1H",
              "RRULE:FREQ=DAILY;COUNT=3", "SUMMARY:Night")
    got = occ_utc(tmp_path, body, "2026-03-20T00:00", "2026-04-10T00:00")
    assert [s for s, _, _ in got] == [u(2026, 3, 28, 1, 30), u(2026, 3, 29, 1, 30), u(2026, 3, 30, 0, 30)]


def test_dtend_gives_exact_elapsed_time_across_dst(tmp_path):
    # 23:00 EDT -> 03:00 EST over the fall-back is 5 real hours, not 4
    body = ev("DTSTART;TZID=America/New_York:20261031T230000", "DTEND;TZID=America/New_York:20261101T030000",
              "SUMMARY:Long night")
    (s, e, _), = occ_utc(tmp_path, body, "2026-10-30T00:00", "2026-11-03T00:00")
    assert e - s == timedelta(hours=5)
    # and over the spring-forward it is 3, not 4: 23:00 EST -> 03:00 EDT
    body = ev("DTSTART;TZID=America/New_York:20260307T230000", "DTEND;TZID=America/New_York:20260308T030000",
              "SUMMARY:Short night")
    (s, e, _), = occ_utc(tmp_path, body, "2026-03-06T00:00", "2026-03-10T00:00")
    assert e - s == timedelta(hours=3)


def test_duration_in_days_is_nominal_but_hours_are_exact(tmp_path):
    body = ev("DTSTART;TZID=America/New_York:20260307T120000", "DURATION:P1D", "SUMMARY:A day")
    (s, e, _), = occ_utc(tmp_path, body, "2026-03-06T00:00", "2026-03-10T00:00")
    assert e - s == timedelta(hours=23)  # noon to noon across the spring-forward
    body = ev("DTSTART;TZID=America/New_York:20260307T120000", "DURATION:PT24H", "SUMMARY:24 hours")
    (s, e, _), = occ_utc(tmp_path, body, "2026-03-06T00:00", "2026-03-10T00:00")
    assert e - s == timedelta(hours=24)


def test_istanbul_has_no_dst_so_instants_never_move(tmp_path):
    body = ev("DTSTART;TZID=Europe/Istanbul:20260105T100000", "DURATION:PT1H",
              "RRULE:FREQ=WEEKLY;COUNT=60", "SUMMARY:Istanbul")
    got = occ_utc(tmp_path, body, "2026-01-01T00:00", "2027-03-01T00:00")
    assert len(got) == 60 and {s.hour for s, _, _ in got} == {7}


def test_utc_dtstart_recurs_in_utc_not_local_wall_time(tmp_path):
    body = ev("DTSTART:20260320T090000Z", "DURATION:PT1H", "RRULE:FREQ=WEEKLY;COUNT=4", "SUMMARY:UTC")
    got = occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-05-01T00:00", tz="Europe/Berlin")
    assert {s.hour for s, _, _ in got} == {9}  # Z means the same instant of day all year


def test_until_in_utc_is_inclusive_at_the_exact_instant(tmp_path):
    base = ["DTSTART;TZID=Europe/Berlin:20260323T090000", "DURATION:PT1H", "SUMMARY:U"]
    # the Mar 30 occurrence is 09:00 CEST = 07:00Z
    got = occ_utc(tmp_path, ev(*base, "RRULE:FREQ=WEEKLY;UNTIL=20260330T070000Z"), "2026-03-01T00:00", "2026-06-01T00:00")
    assert len(got) == 2
    got = occ_utc(tmp_path, ev(*base, "RRULE:FREQ=WEEKLY;UNTIL=20260330T065959Z"), "2026-03-01T00:00", "2026-06-01T00:00")
    assert len(got) == 1
    got = occ_utc(tmp_path, ev(*base, "RRULE:FREQ=WEEKLY;UNTIL=20260330"), "2026-03-01T00:00", "2026-06-01T00:00")
    assert len(got) == 2  # a DATE until includes that whole day


def test_exdate_and_rdate_and_count(tmp_path):
    body = ev("DTSTART;TZID=Europe/Berlin:20260302T090000", "DURATION:PT1H", "RRULE:FREQ=DAILY;COUNT=5",
              "EXDATE;TZID=Europe/Berlin:20260303T090000", "RDATE;TZID=Europe/Berlin:20260310T150000",
              "SUMMARY:X")
    got = occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-03-31T00:00", tz="Europe/Berlin")
    days = [s.astimezone(require_zone("Europe/Berlin")).strftime("%d %H") for s, _, _ in got]
    # COUNT=5 counts the excluded day: Mar 2,3,4,5,6 minus Mar 3, plus the RDATE
    assert days == ["02 09", "04 09", "05 09", "06 09", "10 15"]


def test_exdate_as_utc_matches_a_zoned_occurrence(tmp_path):
    body = ev("DTSTART;TZID=Europe/Berlin:20260302T090000", "DURATION:PT1H", "RRULE:FREQ=DAILY;COUNT=3",
              "EXDATE:20260303T080000Z", "SUMMARY:X")
    got = occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-03-31T00:00")
    assert len(got) == 2


def test_date_valued_exdate_removes_a_timed_occurrence(tmp_path):
    body = ev("DTSTART;TZID=Europe/Berlin:20260302T090000", "DURATION:PT1H", "RRULE:FREQ=DAILY;COUNT=3",
              "EXDATE;VALUE=DATE:20260303", "SUMMARY:X")
    assert len(occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-03-31T00:00")) == 2


def test_recurrence_id_override_moves_and_cancelled_override_removes(tmp_path):
    master = ev("DTSTART;TZID=Europe/Berlin:20260302T090000", "DURATION:PT1H", "RRULE:FREQ=DAILY;COUNT=4",
                "SUMMARY:Series")
    moved = ["BEGIN:VEVENT", "UID:u1", "DTSTAMP:20260101T000000Z", "RECURRENCE-ID;TZID=Europe/Berlin:20260303T090000",
             "DTSTART;TZID=Europe/Berlin:20260303T160000", "DTEND;TZID=Europe/Berlin:20260303T170000",
             "SUMMARY:Moved", "END:VEVENT"]
    gone = ["BEGIN:VEVENT", "UID:u1", "DTSTAMP:20260101T000000Z", "RECURRENCE-ID;TZID=Europe/Berlin:20260304T090000",
            "DTSTART;TZID=Europe/Berlin:20260304T090000", "DURATION:PT1H", "STATUS:CANCELLED",
            "SUMMARY:Gone", "END:VEVENT"]
    got = occ_utc(tmp_path, master + moved + gone, "2026-03-01T00:00", "2026-03-31T00:00")
    assert [(s.hour, n) for s, _, n in got] == [(8, "Series"), (15, "Moved"), (8, "Series")]
    got = occ_utc(tmp_path, master + moved + gone, "2026-03-01T00:00", "2026-03-31T00:00", include_cancelled=True)
    assert "Gone" in [n for _, _, n in got]


def test_cancelled_event_is_hidden_unless_asked(tmp_path):
    body = ev("DTSTART:20260302T090000Z", "DURATION:PT1H", "STATUS:CANCELLED", "SUMMARY:C")
    assert occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-03-31T00:00") == []
    assert len(occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-03-31T00:00", include_cancelled=True)) == 1


def test_floating_times_are_read_in_the_requested_zone(tmp_path):
    body = ev("DTSTART:20260302T090000", "DTEND:20260302T100000", "SUMMARY:F")
    (s, _, _), = occ_utc(tmp_path, body, "2026-03-02T00:00", "2026-03-03T00:00", tz="Europe/Istanbul")
    assert s == u(2026, 3, 2, 6)
    (s, _, _), = occ_utc(tmp_path, body, "2026-03-02T00:00", "2026-03-03T00:00", tz="America/New_York")
    assert s == u(2026, 3, 2, 14)


def test_all_day_events_are_whole_local_days_in_any_zone(tmp_path):
    body = ev("DTSTART;VALUE=DATE:20260302", "DTEND;VALUE=DATE:20260304", "SUMMARY:Trip")
    (s, e, _), = occ_utc(tmp_path, body, "2026-03-02T00:00", "2026-03-03T00:00", tz="America/New_York")
    assert (s, e) == (u(2026, 3, 2, 5), u(2026, 3, 4, 5))
    assert occ_utc(tmp_path, body, "2026-03-04T00:00", "2026-03-05T00:00") == []  # DTEND is exclusive
    assert len(occ_utc(tmp_path, body, "2026-03-03T23:00", "2026-03-05T00:00")) == 1


def test_all_day_weekly_recurrence_and_exdate(tmp_path):
    body = ev("DTSTART;VALUE=DATE:20260302", "RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE;VALUE=DATE:20260309", "SUMMARY:A")
    got = occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-04-30T00:00")
    assert [s.day for s, _, _ in got] == [2, 16, 23]


def test_event_that_started_before_the_window_is_included(tmp_path):
    body = ev("DTSTART:20260302T220000Z", "DURATION:PT30H", "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Long")
    # Tuesday 04:00Z is inside the Mar 2 22:00Z + 30h occurrence
    assert len(occ_utc(tmp_path, body, "2026-03-03T04:00", "2026-03-03T05:00")) == 1
    assert len(occ_utc(tmp_path, body, "2026-03-10T04:00", "2026-03-10T05:00")) == 1
    assert occ_utc(tmp_path, body, "2026-03-04T05:00", "2026-03-04T06:00") == []


def test_overlap_semantics_at_the_window_edges(tmp_path):
    body = ev("DTSTART:20260302T090000Z", "DTEND:20260302T100000Z", "SUMMARY:Edge")
    assert occ_utc(tmp_path, body, "2026-03-02T10:00", "2026-03-02T11:00") == []  # touches only
    assert occ_utc(tmp_path, body, "2026-03-02T08:00", "2026-03-02T09:00") == []
    assert len(occ_utc(tmp_path, body, "2026-03-02T09:59", "2026-03-02T11:00")) == 1


def test_monthly_last_friday_across_a_dst_change(tmp_path):
    body = ev("DTSTART;TZID=Europe/Berlin:20260227T170000", "DURATION:PT1H",
              "RRULE:FREQ=MONTHLY;BYDAY=-1FR;COUNT=3", "SUMMARY:Drinks")
    got = occ_utc(tmp_path, body, "2026-02-01T00:00", "2026-06-01T00:00")
    assert [s for s, _, _ in got] == [u(2026, 2, 27, 16), u(2026, 3, 27, 16), u(2026, 4, 24, 15)]


def test_thousand_year_old_and_far_future_dates_do_not_crash(tmp_path):
    body = ev("DTSTART:19000101T090000Z", "DURATION:PT1H", "RRULE:FREQ=YEARLY", "SUMMARY:Old")
    assert len(occ_utc(tmp_path, body, "2026-01-01T00:00", "2026-12-31T00:00")) == 1
    body = ev("DTSTART:20260101T090000Z", "DURATION:PT1H", "RRULE:FREQ=YEARLY", "SUMMARY:Far")
    assert len(occ_utc(tmp_path, body, "2300-01-01T00:00", "2300-12-31T00:00")) == 1


@pytest.mark.parametrize("tz", ["Europe/Istanbul", "America/New_York", "Pacific/Auckland", "Asia/Kolkata"])
def test_same_instants_whatever_zone_you_ask_in(tmp_path, tz):
    body = ev("DTSTART;TZID=Europe/Berlin:20260320T090000", "DURATION:PT1H", "RRULE:FREQ=WEEKLY;COUNT=4", "SUMMARY:S")
    got = occ_utc(tmp_path, body, "2026-03-01T00:00", "2026-05-01T00:00", tz=tz)
    assert [s for s, _, _ in got] == [u(2026, 3, 20, 8), u(2026, 3, 27, 8), u(2026, 4, 3, 7), u(2026, 4, 10, 7)]
