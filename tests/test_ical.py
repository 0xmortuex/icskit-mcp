from datetime import datetime

import pytest
from conftest import vcal

from icskit_mcp.ical import (
    ICalError,
    escape_text,
    fold_line,
    parse,
    parse_datetime,
    parse_duration,
    parse_utc_offset,
    split_physical,
    split_text_list,
    unescape_text,
)


def test_split_keeps_endings_and_ignores_unicode_line_separators():
    parts = split_physical("a\r\nb\nc\rd e\x0cf")
    assert parts == ["a\r\n", "b\n", "c\r", "d e\x0cf"]


def test_unfolding_joins_continuations_and_drops_one_space():
    p = parse(vcal("BEGIN:VEVENT", "SUMMARY:Hello", " , world", "\tand tab", "END:VEVENT"))
    ev = p.roots[0].sub("VEVENT")[0]
    s = ev.get("SUMMARY")
    assert s.value == "Hello, worldand tab"
    assert s.last - s.first == 2  # the span covers all three physical lines


def test_parameters_quoting_and_colons_inside_quotes():
    p = parse(vcal("BEGIN:VEVENT", 'ATTENDEE;CN="Doe; John: Jr.";ROLE=CHAIR:mailto:j@x.org', "END:VEVENT"))
    a = p.roots[0].sub("VEVENT")[0].get("ATTENDEE")
    assert a.param("CN") == "Doe; John: Jr."
    assert a.param("role") == "CHAIR"
    assert a.value == "mailto:j@x.org"


def test_multi_valued_parameter():
    p = parse(vcal("BEGIN:VEVENT", 'ATTENDEE;MEMBER="mailto:a@x","mailto:b@x":mailto:c@x', "END:VEVENT"))
    a = p.roots[0].sub("VEVENT")[0].get("ATTENDEE")
    assert a.params["MEMBER"] == ["mailto:a@x", "mailto:b@x"]


def test_text_escaping_roundtrip():
    raw = "a, b; c\\ d\nline2"
    esc = escape_text(raw)
    assert esc == "a\\, b\\; c\\\\ d\\nline2"
    assert unescape_text(esc) == raw
    assert unescape_text("x\\Ny") == "x\ny"


def test_text_list_split_respects_escaped_commas():
    assert split_text_list("work,home\\, sweet,x") == ["work", "home, sweet", "x"]


def test_group_prefix_and_lowercase_names():
    p = parse(vcal("BEGIN:VEVENT", "item1.summary:grouped", "END:VEVENT"))
    assert p.roots[0].sub("VEVENT")[0].get("SUMMARY").value == "grouped"


def test_bom_is_tolerated():
    p = parse("﻿" + vcal("BEGIN:VEVENT", "UID:1", "END:VEVENT"))
    assert p.roots[0].name == "VCALENDAR"


def test_malformed_line_is_reported_not_swallowed():
    p = parse(vcal("BEGIN:VEVENT", "this is junk", "UID:1", "END:VEVENT"))
    assert any("malformed property" in w and "line 5" in w for w in p.warnings)
    assert p.roots[0].sub("VEVENT")[0].get("UID").value == "1"


def test_lone_cr_line_endings():
    p = parse("BEGIN:VCALENDAR\rVERSION:2.0\rBEGIN:VEVENT\rUID:1\rEND:VEVENT\rEND:VCALENDAR\r")
    assert p.roots[0].sub("VEVENT")[0].get("UID").value == "1"


def test_unbalanced_structure_raises_with_line_numbers():
    with pytest.raises(ICalError, match="never closed"):
        parse("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\n")
    with pytest.raises(ICalError, match="does not match"):
        parse("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
    with pytest.raises(ICalError, match="no BEGIN:VCALENDAR"):
        parse("hello world\r\n")


def test_datetime_and_duration_parsing():
    assert parse_datetime("20260302T093000") == (datetime(2026, 3, 2, 9, 30), False)
    assert parse_datetime("20260302T093000Z")[1] is True
    with pytest.raises(ICalError):
        parse_datetime("2026-03-02 09:30")
    with pytest.raises(ICalError):
        parse_datetime("20261340T000000")
    d = parse_duration("P1W2DT3H4M5S")
    assert (d.days, d.seconds) == (9, 3 * 3600 + 4 * 60 + 5)
    assert parse_duration("-PT15M").nominal().total_seconds() == -900
    for bad in ("P", "PT", "1H", "P1H"):
        with pytest.raises(ICalError):
            parse_duration(bad)


def test_utc_offset():
    assert parse_utc_offset("+0530").total_seconds() == 19800
    assert parse_utc_offset("-0800").total_seconds() == -28800
    with pytest.raises(ICalError):
        parse_utc_offset("+5")


def test_fold_line_never_splits_a_utf8_character_and_limits_octets():
    line = "SUMMARY:" + "çğış" * 30 + "\U0001F600" * 5
    pieces = fold_line(line)
    assert all(len(p.encode("utf-8")) <= 75 for p in pieces)
    assert all(p.startswith(" ") for p in pieces[1:])
    assert "".join([pieces[0]] + [p[1:] for p in pieces[1:]]) == line
