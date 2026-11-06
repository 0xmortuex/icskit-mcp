"""Hostile and damaged calendars: every problem is reported, none is swallowed,
and one bad file or event never takes the rest down."""

import os
import shutil

import pytest
from conftest import FIX, vcal, write

from icskit_mcp import server
from icskit_mcp.ical import CalendarError


@pytest.fixture
def hostile(tmp_path):
    shutil.copy(FIX / "hostile.ics", tmp_path / "hostile.ics")
    shutil.copy(FIX / "google.ics", tmp_path / "google.ics")
    server.configure(tmp_path)
    yield tmp_path
    server._lib = None


def test_bad_events_are_skipped_with_a_reason_and_good_ones_survive(hostile):
    out = server.calendar_overview()
    assert "Problems (6)" in out
    for needle in ("no DTSTART", "FREQ=HOURLY is not supported", "DTEND is before DTSTART",
                   "Mars/Olympus_Mons", "invalid DATE-TIME", "line 4: malformed property"):
        assert needle in out, needle
    agenda = server.agenda("2026-03-02", "2026-03-02", "UTC")
    assert "Fine event" in agenda and "Team standup" in agenda
    assert "Warnings:" in agenda  # reads also surface the problems


def test_damaged_files_are_listed_not_fatal(tmp_path):
    write(tmp_path / "good.ics", vcal("BEGIN:VEVENT", "UID:1", "DTSTART:20260302T090000Z", "DURATION:PT1H",
                                      "SUMMARY:Still here", "END:VEVENT"))
    write(tmp_path / "truncated.ics", "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:2\r\n")
    write(tmp_path / "notcal.ics", "hello, I am a text file\r\n")
    write(tmp_path / "empty.ics", "")
    (tmp_path / "latin1.ics").write_bytes(
        b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Caf\xe9\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    (tmp_path / "binary.ics").write_bytes(bytes(range(256)) * 20)
    mismatch = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nEND:VALARM\r\nEND:VCALENDAR\r\n"
    write(tmp_path / "mismatch.ics", mismatch)
    server.configure(tmp_path)
    try:
        out = server.calendar_overview()
        assert "good.ics" in out
        assert "truncated.ics: BEGIN:VEVENT on line 2 is never closed" in out
        assert "notcal.ics: no BEGIN:VCALENDAR" in out
        assert "empty.ics: no BEGIN:VCALENDAR" in out
        assert "latin1.ics: not valid UTF-8" in out
        assert "binary.ics" in out
        assert "mismatch.ics: line 3: END:VALARM does not match" in out
        assert "Still here" in server.agenda("2026-03-02", "2026-03-02")
        with pytest.raises(CalendarError, match="never closed"):
            server.create_event("truncated.ics", "x", "2026-03-02", dry_run=True)
    finally:
        server._lib = None


def test_a_file_fixed_on_disk_is_picked_up_again(tmp_path):
    p = write(tmp_path / "a.ics", "garbage")
    server.configure(tmp_path)
    try:
        assert "no BEGIN:VCALENDAR" in server.calendar_overview()
        write(p, vcal("BEGIN:VEVENT", "UID:1", "DTSTART:20260302T090000Z", "DURATION:PT1H", "SUMMARY:Fixed", "END:VEVENT"))
        # make sure the (mtime, size) stamp differs even on coarse clocks
        os.utime(p, ns=(1, 1))
        assert "Fixed" in server.agenda("2026-03-02", "2026-03-02")
        p.unlink()
        assert "No .ics files" in server.calendar_overview()
    finally:
        server._lib = None


def test_odd_but_legal_content(tmp_path):
    big = "x" * 200_000
    write(tmp_path / "odd.ics", vcal(
        "BEGIN:VEVENT", "UID:1", "DTSTART:20260302T090000Z", "DTEND:20260302T090000Z", "SUMMARY:Instant",
        "DESCRIPTION:" + big, "END:VEVENT",
        "BEGIN:VEVENT", "UID:2", "DTSTART;VALUE=DATE:20260302", "SUMMARY:Date no end", "END:VEVENT",
        "BEGIN:VEVENT", "UID:3", "DTSTART:20260302T100000Z", "SUMMARY:No end at all", "END:VEVENT",
        "BEGIN:VEVENT", "UID:4", "DTSTART;VALUE=DATE:20260302", "DTEND;VALUE=DATE:20260302", "SUMMARY:Zero days", "END:VEVENT",
        "BEGIN:VEVENT", "DTSTART:20260302T110000Z", "DURATION:PT1H", "SUMMARY:No uid", "END:VEVENT",
        "BEGIN:VEVENT", "UID:6", "DTSTART:20260302T120000Z", "DURATION:PT1H", "RRULE:FREQ=DAILY;UNTIL=20200101T000000Z",
        "SUMMARY:Until before start", "END:VEVENT",
        "BEGIN:VEVENT", "UID:7", "DTSTART:20260302T130000Z", "DURATION:PT1H",
        "RRULE:FREQ=MONTHLY;BYMONTHDAY=31;BYMONTH=2", "SUMMARY:Never", "END:VEVENT",
    ))
    server.configure(tmp_path)
    try:
        out = server.agenda("2026-03-02", "2026-03-02", "UTC")
        # DTSTART is always the first instance, even when the rule can never match it
        for name in ("Instant", "Date no end", "No end at all", "Zero days", "No uid", "Never"):
            assert name in out, name
        assert "Until before start" not in out  # UNTIL earlier than DTSTART: the series is empty
        assert "Problems" not in server.calendar_overview()  # a missing UID is tolerated
    finally:
        server._lib = None


def test_unreachable_rules_do_not_hang(tmp_path):
    write(tmp_path / "r.ics", vcal(
        "BEGIN:VEVENT", "UID:7", "DTSTART:20260302T130000Z", "DURATION:PT1H",
        "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", "SUMMARY:Never", "END:VEVENT"))
    server.configure(tmp_path)
    try:
        out = server.next_occurrences("7", 3, "2026-01-01", "UTC")
        assert "Never" in out and ("Only 1 occurrence" in out or "No further" in out)
        assert "Never" in server.agenda("2026-03-02", "2026-03-02", "UTC")  # DTSTART itself is the first instance
        assert "No events" in server.agenda("2027-01-01", "2030-12-31", "UTC")
    finally:
        server._lib = None


def test_sandbox_blocks_escapes_and_symlinks(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside.ics"
    write(outside, vcal("BEGIN:VEVENT", "UID:o", "DTSTART:20260302T090000Z", "DURATION:PT1H", "SUMMARY:Secret", "END:VEVENT"))
    server.configure(inside)
    try:
        assert "Secret" not in server.calendar_overview()
        for bad in ("../outside.ics", str(outside), "..\\outside.ics", "sub/../../outside.ics"):
            with pytest.raises(CalendarError, match="outside the allowed directory|not a calendar file"):
                server.update_event("o", calendar=bad, summary="x", dry_run=True)
        link = inside / "link.ics"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlinks on this machine")
        assert "Secret" not in server.calendar_overview()
        with pytest.raises(CalendarError):
            server.update_event("o", calendar="link.ics", summary="x", in_place=True)
        assert "SUMMARY:Secret" in outside.read_text("utf-8")
    finally:
        server._lib = None


def test_missing_dir_is_a_clear_error(tmp_path):
    with pytest.raises(CalendarError, match="not a directory"):
        server.configure(tmp_path / "nope")


def test_unconfigured_server_says_how_to_start(monkeypatch):
    monkeypatch.setattr(server, "_lib", None)
    with pytest.raises(CalendarError, match="--dir"):
        server.calendar_overview()


def test_regex_is_validated_and_search_needs_a_criterion(hostile):
    with pytest.raises(CalendarError, match="invalid regex"):
        server.search_events(regex="(unclosed")
    with pytest.raises(CalendarError, match="at least one"):
        server.search_events()
    with pytest.raises(CalendarError, match="both start and end"):
        server.search_events(query="x", start="2026-03-01")
    assert "1 matching" in server.search_events(regex=r"^Fine\b")
    assert "0 matching" in server.search_events(query="zzz-no-such-thing")
