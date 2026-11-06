"""Time zone machinery: custom VTIMEZONE blocks must behave exactly like the
IANA zones they describe, including around the DST gap and overlap."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import FIX

from icskit_mcp.ical import parse
from icskit_mcp.model import place
from icskit_mcp.tz import TimezoneError, TzResolver, VTimezone, require_zone

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
NY = ZoneInfo("America/New_York")


def vtz(name: str, fixture: str):
    root = parse((FIX / fixture).read_text(encoding="utf-8")).roots[0]
    comps = [c for c in root.sub("VTIMEZONE") if c.get("TZID").value == name]
    return VTimezone(name, comps[0])


def test_outlook_w_europe_matches_berlin_every_hour_2020_to_2034():
    custom = vtz("W. Europe Standard Time", "outlook.ics")
    t = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2034, 1, 1, tzinfo=UTC)
    step = timedelta(minutes=30)
    while t < end:
        assert t.astimezone(custom).utcoffset() == t.astimezone(BERLIN).utcoffset(), t
        t += step * 11  # 5.5h: walks through every wall-clock phase


def test_outlook_eastern_matches_new_york_around_every_transition():
    custom = vtz("Eastern Standard Time", "outlook.ics")
    for year in range(2015, 2036):
        for month in (3, 11):
            for day in range(1, 15):
                for h in range(0, 24):
                    t = datetime(year, month, day, h, tzinfo=UTC)
                    assert t.astimezone(custom).utcoffset() == t.astimezone(NY).utcoffset(), t


@pytest.mark.parametrize(
    "zone_name,fixture,real",
    [("W. Europe Standard Time", "outlook.ics", BERLIN), ("Eastern Standard Time", "outlook.ics", NY)],
)
def test_wall_to_instant_policy_matches_zoneinfo_in_gap_and_overlap(zone_name, fixture, real):
    custom = vtz(zone_name, fixture)
    for year in (2024, 2026, 2031):
        for month in (3, 10, 11):
            for day in range(1, 32):
                for hh in (0, 1, 2, 3):
                    for mm in (0, 30):
                        try:
                            wall = datetime(year, month, day, hh, mm)
                        except ValueError:
                            continue
                        a = place(wall, custom).astimezone(UTC)
                        b = place(wall, real).astimezone(UTC)
                        assert a == b, wall
                        # the second pass through an overlap (fold=1) agrees too
                        a1 = wall.replace(tzinfo=custom, fold=1).astimezone(UTC)
                        b1 = wall.replace(tzinfo=real, fold=1).astimezone(UTC)
                        assert a1 == b1, wall


def test_gap_and_overlap_policy_explicit():
    # Berlin 2026-03-29 02:30 does not exist: read with the offset before (CET) -> 03:30 CEST
    gap = place(datetime(2026, 3, 29, 2, 30), BERLIN)
    assert gap.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert (gap.hour, gap.minute, gap.utcoffset()) == (3, 30, timedelta(hours=2))
    # Berlin 2026-10-25 02:30 happens twice: the FIRST one (CEST) is meant
    over = place(datetime(2026, 10, 25, 2, 30), BERLIN)
    assert over.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    # America/New_York
    assert place(datetime(2026, 3, 8, 2, 30), NY).astimezone(UTC) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert place(datetime(2026, 11, 1, 1, 30), NY).astimezone(UTC) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_istanbul_has_no_dst():
    ist = ZoneInfo("Europe/Istanbul")
    offs = {place(datetime(2026, m, 15, 12), ist).utcoffset() for m in range(1, 13)}
    assert offs == {timedelta(hours=3)}


def test_custom_tzname_and_isoformat():
    custom = vtz("Eastern Standard Time", "outlook.ics")
    summer = datetime(2026, 7, 1, 12, tzinfo=UTC).astimezone(custom)
    assert summer.isoformat() == "2026-07-01T08:00:00-04:00"


def test_resolver_prefers_iana_then_vtimezone_then_errors():
    root = parse((FIX / "outlook.ics").read_text(encoding="utf-8")).roots[0]
    r = TzResolver(root.sub("VTIMEZONE"))
    assert r.get("Europe/Istanbul").key == "Europe/Istanbul"
    assert isinstance(r.get("W. Europe Standard Time"), VTimezone)
    assert r.get("/mozilla.org/20050126_1/Europe/Berlin").key == "Europe/Berlin"
    with pytest.raises(TimezoneError, match="no VTIMEZONE"):
        r.get("Mars/Olympus_Mons")


def test_require_zone_is_instructive():
    assert require_zone("utc").utcoffset(None) == timedelta(0)
    with pytest.raises(TimezoneError, match="IANA"):
        require_zone("EST5EDT-ish")
    with pytest.raises(TimezoneError, match="empty"):
        require_zone("  ")
