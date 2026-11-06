"""Read tools against the Google / Apple / Outlook style fixtures."""

import pytest

from icskit_mcp import server
from icskit_mcp.ical import CalendarError


def test_overview_lists_files_names_zones_and_spans(configured):
    out = server.calendar_overview()
    assert "3 calendar file(s)" in out
    assert '- google.ics "Work (Google)" : 4 events, 1 recurring, 1 modified instances' in out
    assert "time zones: Europe/Istanbul, UTC, all-day" in out
    assert "time zones: Eastern Standard Time, W. Europe Standard Time" in out
    assert "(and 1 series that repeat with no end)" in out
    assert "Problems" not in out


def test_agenda_shows_zone_everywhere_and_marks_recurrence(configured):
    out = server.agenda("2026-03-02", "2026-03-06", "Europe/Istanbul")
    assert "zone Europe/Istanbul" in out and "UTC+03:00" in out
    assert "Mon 2026-03-02\n  10:00-10:30 +03  Team standup [recurring] @ Room 4, HQ" in out
    assert "Fri 2026-03-06" not in out  # EXDATE
    assert "Thu 2026-03-05\n  18:00-19:00 +03  Free-busy tentative block [shown as free]" in out


def test_agenda_same_data_different_zone(configured):
    ist = server.agenda("2026-03-04", "2026-03-04", "Europe/Istanbul")
    ber = server.agenda("2026-03-04", "2026-03-04", "Europe/Berlin")
    assert "10:00-10:30 +03  Team standup" in ist and "08:00-08:30 CET  Team standup" in ber
    # Outlook's custom W. Europe zone: 10:00 local = 09:00 Berlin = 11:00 Istanbul
    assert "12:00-13:00 +03  Weekly review" in ist and "10:00-11:00 CET  Weekly review" in ber


def test_agenda_custom_vtimezone_changes_offset_after_dst(configured):
    # Weekly review is 10:00 in "W. Europe Standard Time"; Europe changes clocks on Mar 29
    out = server.agenda("2026-03-25", "2026-04-02", "UTC")
    assert "09:00-10:00 UTC  Weekly review" in out  # Mar 25: CET
    assert "08:00-09:00 UTC  Weekly review" in out  # Apr 1: CEST


def test_cancelled_and_all_day_handling(configured):
    out = server.agenda("2026-03-12", "2026-03-22", "UTC")
    assert "Cancelled workshop" not in out
    assert "Cancelled workshop" in server.agenda("2026-03-12", "2026-03-12", "UTC", include_cancelled=True)
    assert "all day (2026-03-20 .. 2026-03-22)  Spring break" in out  # DTEND 03-23 is exclusive


def test_agenda_multi_day_all_day_event_is_not_shifted_by_tz(configured):
    for tz in ("Pacific/Auckland", "Pacific/Pago_Pago"):
        out = server.agenda("2026-03-20", "2026-03-22", tz)
        assert "all day (2026-03-20 .. 2026-03-22)  Spring break" in out


def test_agenda_limit_and_calendar_filter(configured):
    out = server.agenda("2026-03-01", "2026-04-30", "UTC", limit=3)
    assert "not shown" in out
    only = server.agenda("2026-03-01", "2026-03-31", "UTC", calendars=["outlook"])
    assert "Weekly review" in only and "Team standup" not in only
    with pytest.raises(CalendarError, match="Available: .*google.ics"):
        server.agenda("2026-03-01", "2026-03-31", "UTC", calendars=["nope"])


def test_search_by_text_attendee_location_and_regex(configured):
    assert "Team standup" in server.search_events(query="BLOCKERS")  # description, case-insensitive
    assert "Team standup" in server.search_events(attendee="bob@example")
    assert "Team standup" in server.search_events(attendee="doe, jane")
    assert "Dinner with Anna" in server.search_events(location="linde")
    assert "Weekly review" in server.search_events(regex=r"review$")
    out = server.search_events(query="standup")
    assert "2 matching" in out and "modified instance" in out
    assert "1 matching" in server.search_events(query="standup", location="HQ")  # the moved copy has no location
    assert "0 matching" in server.search_events(query="standup", location="nowhere")


def test_search_in_window_expands_occurrences(configured):
    out = server.search_events(query="standup", start="2026-03-02", end="2026-03-13", tz="Europe/Istanbul")
    # Mar 2, 4, (6 is an EXDATE), 9 moved to 14:30, 11, 13
    assert "5 matching occurrence(s)" in out and out.count("Team standup") == 5
    assert "14:30-15:00 +03  Team standup (moved) [modified instance]" in out


def test_next_occurrences_applies_overrides_and_exdates(configured):
    out = server.next_occurrences("standup-001@google.com", 4, "2026-03-04T12:00", "Europe/Istanbul")
    lines = [ln.strip() for ln in out.splitlines()[1:]]
    assert [ln[:16] for ln in lines] == [
        "2026-03-09 14:30", "2026-03-11 10:00", "2026-03-13 10:00", "2026-03-16 10:00"
    ]  # the 9th is the moved instance; the 6th is gone
    out = server.next_occurrences("market-day@apple", 10, "2026-01-01", "Europe/Berlin")
    assert out.count("all day") == 3 and "Only 3 occurrence(s) left" in out
    assert "2026-03-14 all day" in out and "2026-04-11" in out and "2026-05-09" in out
    with pytest.raises(CalendarError, match="between 1 and 500"):
        server.next_occurrences("market-day@apple", 0)


def test_next_occurrences_of_a_finished_series(configured):
    out = server.next_occurrences("night-shift@apple", 3, "2027-01-01", "Europe/Berlin")
    assert "No further occurrences" in out


def test_monthly_nth_weekday_in_a_real_fixture_dst_zone(configured):
    out = server.next_occurrences("market-day@apple", 3, "2026-03-14", "Pacific/Kiritimati")
    assert "2026-03-14" in out  # all-day dates do not move with the zone


def test_export_markdown_week(configured, caldir):
    md = server.export_agenda_markdown("2026-03-09", "Europe/Istanbul")
    assert md.startswith("# Agenda 2026-03-09 to 2026-03-15 (Europe/Istanbul)")
    assert "## Mon 2026-03-09\n- 14:30-15:00 +03 - Team standup (moved)" in md
    assert "## Tue 2026-03-10\n_No events_" in md
    assert md.count("## ") == 7
    out = server.export_agenda_markdown("2026-03-09", "UTC", output_file="week.md")
    assert "(saved to week.md)" in out and (caldir / "week.md").read_text("utf-8").startswith("# Agenda")
    with pytest.raises(CalendarError, match="already exists"):
        server.export_agenda_markdown("2026-03-09", "UTC", output_file="week.md")
    with pytest.raises(CalendarError, match=".md"):
        server.export_agenda_markdown("2026-03-09", "UTC", output_file="evil.ics")
    with pytest.raises(CalendarError, match="outside the allowed"):
        server.export_agenda_markdown("2026-03-09", "UTC", output_file="../week.md")
    with pytest.raises(CalendarError, match="must be a date"):
        server.export_agenda_markdown("2026-03-09T10:00", "UTC")


def test_window_and_date_argument_validation(configured):
    with pytest.raises(CalendarError, match="longer than"):
        server.agenda("2000-01-01", "2030-01-01", "UTC")
    for bad in ("", "2026-13-01", "03/02/2026", "9am"):
        with pytest.raises(CalendarError):
            server.agenda(bad, "2026-03-02", "UTC")
    # an offset in the argument is honoured and converted into tz
    out = server.agenda("2026-03-02T09:55Z", "2026-03-02T13:05+03:00", "UTC")
    assert out.startswith("Agenda 2026-03-02 09:55 UTC -> 2026-03-02 10:05 UTC")
