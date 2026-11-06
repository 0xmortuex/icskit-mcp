"""Write tools: safety defaults and byte-level round-trip fidelity."""

import hashlib
import shutil

import pytest
from conftest import FIX, vcal, write

from icskit_mcp import server, writer
from icskit_mcp.ical import CalendarError, parse, split_physical
from icskit_mcp.library import Library


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blocks(text):
    """Every top-level VEVENT / VTIMEZONE block of a file as raw text, by UID/TZID."""
    parsed = parse(text)
    lines = parsed.lines
    out = {}
    for root in parsed.roots:
        for c in root.children:
            raw = "".join(lines[c.first : c.last + 1])
            if c.name == "VEVENT":
                uid = c.get("UID").value
                rid = c.get("RECURRENCE-ID")
                out[("VEVENT", uid, rid.value if rid else "")] = raw
            else:
                out[(c.name, c.get("TZID").value if c.get("TZID") else "", "")] = raw
    return out


def outside_blocks(text):
    """Everything that is not inside a VEVENT/VTIMEZONE block (header, footer, X- props)."""
    parsed = parse(text)
    inside = set()
    for root in parsed.roots:
        for c in root.children:
            inside.update(range(c.first, c.last + 1))
    return "".join(ln for i, ln in enumerate(parsed.lines) if i not in inside)


@pytest.fixture
def lib(caldir):
    server.configure(caldir)
    yield caldir
    server._lib = None


@pytest.mark.parametrize("name", ["google.ics", "apple.ics", "outlook.ics"])
def test_default_create_never_touches_the_original(lib, name):
    path = lib / name
    before = digest(path)
    out = server.create_event(name, "Dentist", "2026-03-10T15:00", duration_minutes=45, tz="Europe/Istanbul")
    assert digest(path) == before
    new = lib / (name + ".new")
    assert new.exists() and "untouched" in out
    old_b, new_b = blocks(path.read_text("utf-8")), blocks(new.read_text("utf-8"))
    # every pre-existing block is byte-identical, exactly one block is new
    assert all(new_b[k] == v for k, v in old_b.items())
    assert len(new_b) == len(old_b) + 1
    assert outside_blocks(path.read_text("utf-8")) == outside_blocks(new.read_text("utf-8"))


def test_dry_run_writes_nothing_and_shows_a_patch(lib):
    before = {p.name: digest(p) for p in lib.iterdir()}
    out = server.create_event("google.ics", "Dentist", "2026-03-10T15:00", duration_minutes=45,
                              tz="Europe/Istanbul", dry_run=True)
    assert out.startswith("dry run") and "+SUMMARY:Dentist" in out and "+DTSTART;TZID=Europe/Istanbul:20260310T150000" in out
    assert {p.name: digest(p) for p in lib.iterdir()} == before


def test_in_place_modifies_and_the_new_event_is_then_visible(lib):
    server.create_event("google.ics", "Dentist", "2026-03-10T15:00", duration_minutes=45,
                        tz="Europe/Istanbul", in_place=True)
    assert not (lib / "google.ics.new").exists()
    out = server.agenda("2026-03-10", "2026-03-10", "Europe/Istanbul")
    assert "15:00-15:45 +03  Dentist" in out
    assert not [f for f in lib.iterdir() if f.name.endswith(".tmp")]


def test_refuses_to_overwrite_an_existing_copy_and_paths_outside_the_sandbox(lib):
    server.create_event("google.ics", "One", "2026-03-10", tz="UTC")
    with pytest.raises(CalendarError, match="already exists"):
        server.create_event("google.ics", "Two", "2026-03-11", tz="UTC")
    with pytest.raises(CalendarError, match="outside the allowed directory"):
        server.create_event("../escape.ics", "X", "2026-03-11", tz="UTC")
    with pytest.raises(CalendarError, match="outside the allowed directory"):
        server.create_event("google.ics", "X", "2026-03-11", tz="UTC", output_file="../escape.ics")
    with pytest.raises(CalendarError, match="do not also pass"):
        server.create_event("google.ics", "X", "2026-03-11", tz="UTC", in_place=True, output_file="o.ics")
    with pytest.raises(CalendarError, match="original file"):
        server.create_event("google.ics", "X", "2026-03-11", tz="UTC", output_file="google.ics")
    assert not (lib.parent / "escape.ics").exists()


def test_in_place_refuses_if_the_file_changed_since_it_was_read(lib):
    L = server.lib()
    cal = L.get_file("google.ics")
    new_text, uid, _ = writer.build_create(
        cal.text, next(r for r in cal.parsed.roots if r.name == "VCALENDAR"),
        writer.EventFields(summary="Race", start="2026-03-10", tz="UTC"), writer.utcnow())
    (lib / "google.ics").write_bytes(cal.text.encode("utf-8") + b"X-LATE:edit\r\n")
    with pytest.raises(CalendarError, match="changed on disk"):
        writer.commit(L, cal, "google.ics", new_text, True, None, False, uid, True)


def test_create_into_a_new_calendar_file(lib):
    out = server.create_event("fresh.ics", "Hello", "2026-04-01T10:00", end="2026-04-01T11:00", tz="UTC",
                              attendees=["a@example.com"])
    assert "created new calendar fresh.ics" in out
    text = (lib / "fresh.ics").read_bytes().decode()
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0") and text.endswith("END:VCALENDAR\r\n")
    assert "ATTENDEE:mailto:a@example.com" in text
    assert "Hello" in server.agenda("2026-04-01", "2026-04-01", "UTC")
    with pytest.raises(CalendarError, match=".ics"):
        server.create_event("fresh.txt", "x", "2026-04-01", tz="UTC")


def test_line_endings_bom_and_missing_final_newline_are_preserved(tmp_path):
    body = ["BEGIN:VEVENT", "UID:k1", "DTSTAMP:20260101T000000Z", "DTSTART:20260302T090000Z",
            "DTEND:20260302T100000Z", "SUMMARY:Keep", "END:VEVENT"]
    cases = {
        "lf.ics": vcal(*body, nl="\n"),
        "cr.ics": vcal(*body, nl="\r"),
        "bom.ics": "﻿" + vcal(*body),
        "nonl.ics": vcal(*body).rstrip("\r\n"),
    }
    server.configure(tmp_path)
    try:
        for name, text in cases.items():
            write(tmp_path / name, text)
            server.create_event(name, "New", "2026-03-05", tz="UTC")
            new = (tmp_path / (name + ".new")).read_bytes().decode("utf-8")
            nl = "\n" if name == "lf.ics" else "\r" if name == "cr.ics" else "\r\n"
            added = new[len(text.rstrip("\r\n").rsplit("END:VCALENDAR", 1)[0]) :]
            assert "SUMMARY:New" in added
            assert new.startswith(text.rsplit("END:VCALENDAR", 1)[0] if name != "nonl.ics" else text.split("END:VCALENDAR")[0])
            assert all(piece.endswith(nl) for piece in split_physical(added)[:-1])
            if name == "bom.ics":
                assert new.startswith("﻿")
            parse(new)
    finally:
        server._lib = None


def test_long_and_hostile_text_is_folded_escaped_and_cannot_inject_properties(lib):
    summary = ("Ünïcode, semi; back\\slash " + "wörd " * 40).strip()
    server.create_event("google.ics", summary, "2026-03-10", tz="UTC", in_place=True,
                        description="line1\nline2\r\nATTENDEE:mailto:evil@x.org",
                        location="Café; Room 3, Floor 2")
    raw = (lib / "google.ics").read_bytes().decode("utf-8")
    assert all(len(ln.encode("utf-8")) <= 75 for ln in raw.split("\r\n")[-30:])
    assert "evil@x.org" in raw  # present only inside the escaped DESCRIPTION text
    cal = Library(lib).get_file("google.ics")
    ev = [e for e in cal.events if e.summary == summary][0]
    assert ev.description == "line1\nline2\nATTENDEE:mailto:evil@x.org"
    assert ev.location == "Café; Room 3, Floor 2"
    assert ev.attendees == []
    with pytest.raises(CalendarError, match="control character"):
        server.create_event("google.ics", "bad\x00title", "2026-03-10", tz="UTC", dry_run=True)
    with pytest.raises(CalendarError):
        server.create_event("google.ics", "x", "2026-03-10", tz="UTC", dry_run=True,
                            rrule="FREQ=DAILY\r\nATTENDEE:mailto:evil@x.org")


def test_validation_errors_are_actionable(lib):
    def create(**kw):
        args = dict(calendar="google.ics", summary="S", start="2026-03-10T10:00", end="2026-03-10T11:00", tz="UTC", dry_run=True)
        args.update(kw)
        return server.create_event(**args)

    create()
    with pytest.raises(CalendarError, match="end must be after start"):
        create(end="2026-03-10T09:00")
    with pytest.raises(CalendarError, match="end or duration_minutes"):
        create(end=None)
    with pytest.raises(CalendarError, match="not both"):
        create(duration_minutes=30)
    with pytest.raises(CalendarError, match="not an email"):
        create(attendees=["not-an-email"])
    with pytest.raises(CalendarError, match="FREQ"):
        create(rrule="BYDAY=MO")
    with pytest.raises(CalendarError, match="status must be one of"):
        create(status="MAYBE")
    with pytest.raises(CalendarError, match="cannot read start"):
        create(start="next tuesday")
    with pytest.raises(CalendarError, match="summary is required"):
        create(summary=" ")
    with pytest.raises(CalendarError, match="IANA"):
        create(tz="CEST")


def test_create_in_a_dst_gap_is_refused_and_an_overlap_is_flagged(lib):
    with pytest.raises(CalendarError, match="does not exist in Europe/Berlin"):
        server.create_event("google.ics", "Gap", "2026-03-29T02:30", duration_minutes=30, tz="Europe/Berlin", dry_run=True)
    out = server.create_event("google.ics", "Twice", "2026-10-25T02:30", duration_minutes=30, tz="Europe/Berlin", dry_run=True)
    assert "happens twice" in out and "first occurrence" in out


def test_all_day_create_end_is_inclusive_in_the_tool_and_exclusive_in_the_file(lib):
    out = server.create_event("google.ics", "Trip", "2026-05-04", end="2026-05-06", dry_run=True)
    assert "DTSTART;VALUE=DATE:20260504" in out and "DTEND;VALUE=DATE:20260507" in out
    out = server.create_event("google.ics", "Day", "2026-05-04", dry_run=True)
    assert "DTEND;VALUE=DATE:20260505" in out


def test_recurring_create_is_expanded_by_the_reader(lib):
    server.create_event("google.ics", "Gym", "2026-03-02T18:00", duration_minutes=60, tz="Europe/Berlin",
                        rrule="FREQ=WEEKLY;BYDAY=MO,TH;COUNT=4", in_place=True)
    out = server.agenda("2026-03-01", "2026-03-31", "Europe/Berlin")
    assert out.count("Gym [recurring]") == 4


# --- update ----------------------------------------------------------------------------


def test_update_changes_only_the_target_block_and_keeps_unknown_properties(lib):
    path = lib / "apple.ics"
    old = path.read_text("utf-8")
    uid = "market-day@apple"
    server.update_event(uid, summary="Big farmers market", location="Town square", in_place=True)
    new = path.read_text("utf-8")
    ob, nb = blocks(old), blocks(new)
    key = ("VEVENT", uid, "")
    assert {k: v for k, v in ob.items() if k != key} == {k: v for k, v in nb.items() if k != key}
    assert outside_blocks(old) == outside_blocks(new)
    changed = nb[key]
    assert "X-CUSTOM-FLAG:keep-me" in changed and "SUMMARY:Big farmers market" in changed
    assert "LOCATION:Town square" in changed and "SEQUENCE:1" in changed
    assert "RRULE:FREQ=MONTHLY;BYDAY=2SA;COUNT=3" in changed


def test_update_keeps_the_valarm_and_vtimezone_of_apple_events(lib):
    old = (lib / "apple.ics").read_text("utf-8")
    server.update_event("8E3C1B52-0A4E-4B77-9B0F-3D1D2C6A7E01", start="2026-03-21T18:00", in_place=True)
    new = (lib / "apple.ics").read_text("utf-8")
    assert "BEGIN:VALARM" in new and "TRIGGER:-PT30M" in new
    assert blocks(old)[("VTIMEZONE", "Europe/Berlin", "")] == blocks(new)[("VTIMEZONE", "Europe/Berlin", "")]
    out = server.agenda("2026-03-21", "2026-03-21", "Europe/Berlin")
    assert "18:00-20:00 CET  Dinner with Anna" in out  # the 2h length was kept


def test_update_start_keeps_exact_length_across_dst(tmp_path):
    write(tmp_path / "n.ics", vcal(
        "BEGIN:VEVENT", "UID:n1", "DTSTAMP:20260101T000000Z", "DTSTART;TZID=America/New_York:20260307T230000",
        "DTEND;TZID=America/New_York:20260308T010000", "SUMMARY:Late", "END:VEVENT"))
    server.configure(tmp_path)
    try:
        # 23:00 -> 01:00 is 2h. Move so that it crosses the gap: the length stays 2 real hours.
        server.update_event("n1", start="2026-03-07T23:30", in_place=True)
        out = server.agenda("2026-03-07", "2026-03-08", "America/New_York")
        assert "23:30 EST -> 2026-03-08 02:00 EST" not in out
        cal = Library(tmp_path).get_file("n.ics")
        ev = cal.events[0]
        assert (ev.end.instant(None) - ev.start.instant(None)).total_seconds() == 7200
    finally:
        server._lib = None


def test_update_end_duration_attendees_and_removals(lib):
    uid = "standup-001@google.com"
    server.update_event(uid, attendees=["jane@example.com", "new@example.com"], location="", status="TENTATIVE",
                        in_place=True)
    cal = Library(lib).get_file("google.ics")
    ev = cal.series[uid].master
    assert sorted(a.address for a in ev.attendees) == ["jane@example.com", "new@example.com"]
    jane = [a for a in ev.attendees if a.address == "jane@example.com"][0]
    assert jane.partstat == "ACCEPTED" and jane.name == "Doe, Jane"  # kept untouched
    assert ev.location == "" and ev.status == "TENTATIVE"
    server.update_event(uid, duration_minutes=45, in_place=True)
    ev = Library(lib).get_file("google.ics").series[uid].master
    assert ev.end is None and ev.duration.seconds == 2700
    server.update_event(uid, end="2026-03-02T10:50", tz="Europe/Istanbul", in_place=True)
    ev = Library(lib).get_file("google.ics").series[uid].master
    assert ev.duration is None and ev.end.wall.minute == 50
    server.update_event(uid, rrule="", in_place=True)
    assert Library(lib).get_file("google.ics").series[uid].master.rule is None


def test_update_errors(lib):
    with pytest.raises(CalendarError, match="no event with UID"):
        server.update_event("nope", summary="x")
    with pytest.raises(CalendarError, match="nothing to change"):
        server.update_event("standup-001@google.com")
    with pytest.raises(CalendarError, match="together with start or end"):
        server.update_event("standup-001@google.com", tz="UTC")
    with pytest.raises(CalendarError, match="all-day and a timed"):
        server.update_event("standup-001@google.com", start="2026-03-02")
    with pytest.raises(CalendarError, match="summary cannot be empty"):
        server.update_event("standup-001@google.com", summary=" ")


def test_same_uid_in_two_files_needs_a_calendar_choice(lib):
    shutil.copy(lib / "google.ics", lib / "google-copy.ics")
    with pytest.raises(CalendarError, match="several files"):
        server.update_event("standup-001@google.com", summary="x", dry_run=True)
    out = server.update_event("standup-001@google.com", calendar="google-copy.ics", summary="x", dry_run=True)
    assert "google-copy.ics" in out


# --- delete ------------------------------------------------------------------------------


def test_delete_whole_series_removes_master_and_modified_instances(lib):
    old = (lib / "google.ics").read_text("utf-8")
    out = server.delete_event("standup-001@google.com", in_place=True)
    assert "modified instance" in out
    new = (lib / "google.ics").read_text("utf-8")
    assert "standup-001" not in new
    ob, nb = blocks(old), blocks(new)
    assert {k: v for k, v in ob.items() if k[1] != "standup-001@google.com"} == nb


def test_delete_one_occurrence_adds_an_exdate(lib):
    server.delete_event("standup-001@google.com", occurrence="2026-03-11T10:00", tz="Europe/Istanbul", in_place=True)
    out = server.agenda("2026-03-09", "2026-03-13", "Europe/Istanbul")
    assert out.count("Team standup") == 2  # Mon (moved) and Fri; Wed is gone
    assert "Team standup" not in out.split("Wed 2026-03-11")[1].split("\n\n")[0]
    text = (lib / "google.ics").read_text("utf-8")
    assert "EXDATE;TZID=Europe/Istanbul:20260311T100000" in text


def test_delete_an_overridden_occurrence_removes_the_override_too(lib):
    # the Mar 9 instance was moved to 14:30; its original slot is 10:00
    server.delete_event("standup-001@google.com", occurrence="2026-03-09T10:00", tz="Europe/Istanbul", in_place=True)
    assert "moved" not in (lib / "google.ics").read_text("utf-8")
    out = server.agenda("2026-03-09", "2026-03-09", "Europe/Istanbul")
    assert "standup" not in out.lower()


def test_delete_occurrence_errors_list_the_real_occurrences(lib):
    with pytest.raises(CalendarError, match="no occurrence starting at.*Occurrences nearby.*2026-03-11 10:00"):
        server.delete_event("standup-001@google.com", occurrence="2026-03-11T11:00", tz="Europe/Istanbul")
    with pytest.raises(CalendarError, match="not a recurring series"):
        server.delete_event("utc-meeting-002@google.com", occurrence="2026-03-11T10:00", tz="Europe/Istanbul")


def test_delete_all_day_occurrence_by_date(lib):
    server.delete_event("market-day@apple", occurrence="2026-04-11", in_place=True)
    assert "EXDATE;VALUE=DATE:20260411" in (lib / "apple.ics").read_text("utf-8")
    out = server.next_occurrences("market-day@apple", 5, "2026-03-01", "Europe/Berlin")
    assert "2026-04-11" not in out and "2026-05-09" in out


def test_exdate_goes_after_existing_exdates_and_keeps_utc_form(tmp_path):
    write(tmp_path / "u.ics", vcal(
        "BEGIN:VEVENT", "UID:u1", "DTSTAMP:20260101T000000Z", "DTSTART:20260302T090000Z", "DURATION:PT1H",
        "RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20260303T090000Z", "SUMMARY:U", "END:VEVENT"))
    server.configure(tmp_path)
    try:
        server.delete_event("u1", occurrence="2026-03-05T09:00", tz="UTC", in_place=True)
        text = (tmp_path / "u.ics").read_text("utf-8")
        assert text.index("EXDATE:20260303T090000Z") < text.index("EXDATE:20260305T090000Z") < text.index("SUMMARY:U")
    finally:
        server._lib = None


def test_every_fixture_round_trips_untouched_through_a_noop_style_edit(caldir):
    """Create then delete the same event in place: the file must come back byte-identical."""
    server.configure(caldir)
    try:
        for name in ("google.ics", "apple.ics", "outlook.ics"):
            path = caldir / name
            before = path.read_bytes()
            out = server.create_event(name, "Temp", "2026-06-01", tz="UTC", in_place=True)
            uid = [ln for ln in out.splitlines() if ln.startswith("UID: ")][0][5:]
            assert path.read_bytes() != before
            server.delete_event(uid, in_place=True)
            assert path.read_bytes() == before, name
    finally:
        server._lib = None


def test_fixtures_are_what_the_tests_think_they_are():
    for name in ("google.ics", "apple.ics", "outlook.ics", "hostile.ics"):
        assert b"\r\n" in (FIX / name).read_bytes()
