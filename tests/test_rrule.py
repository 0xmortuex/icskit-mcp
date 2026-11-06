"""RFC 5545 section 3.8.5.3 examples, plus a randomized comparison against
python-dateutil as an independent oracle."""

import random
from datetime import date, datetime, timedelta

import pytest

from icskit_mcp.ical import ICalError
from icskit_mcp.rrule import iterate, parse_rule


def dts(start, rule, **kw):
    return list(iterate(parse_rule(rule), start, **kw))


def d(s):
    """'1997-09-02' -> that day at 09:00"""
    y, m, dd = (int(x) for x in s.split("-"))
    return datetime(y, m, dd, 9, 0)


def days(*items):
    return [d(x) for x in items]


S2 = d("1997-09-02")


def test_daily_for_10_occurrences():
    assert dts(S2, "FREQ=DAILY;COUNT=10") == [S2 + timedelta(days=i) for i in range(10)]


def test_daily_until_december_24():
    got = dts(S2, "FREQ=DAILY;UNTIL=19971224T000000Z", until=datetime(1997, 12, 24, 0, 0))
    assert got[0] == S2 and got[-1] == d("1997-12-23") and len(got) == 113


def test_every_other_day_and_every_10_days():
    assert dts(S2, "FREQ=DAILY;INTERVAL=2", hi=d("1997-09-10")) == days(
        "1997-09-02", "1997-09-04", "1997-09-06", "1997-09-08", "1997-09-10"
    )
    assert dts(S2, "FREQ=DAILY;INTERVAL=10;COUNT=5") == days(
        "1997-09-02", "1997-09-12", "1997-09-22", "1997-10-02", "1997-10-12"
    )


def test_everyday_in_january_for_3_years():
    until = datetime(2000, 1, 31, 14, 0)
    got = dts(
        d("1998-01-01"),
        "FREQ=YEARLY;UNTIL=20000131T140000Z;BYMONTH=1;BYDAY=SU,MO,TU,WE,TH,FR,SA",
        until=until,
    )
    assert len(got) == 31 * 3 and got[0] == d("1998-01-01") and got[-1] == d("2000-01-31")
    assert dts(d("1998-01-01"), "FREQ=DAILY;UNTIL=20000131T140000Z;BYMONTH=1", until=until) == got


def test_weekly_for_10_occurrences():
    assert dts(S2, "FREQ=WEEKLY;COUNT=10") == days(
        "1997-09-02", "1997-09-09", "1997-09-16", "1997-09-23", "1997-09-30",
        "1997-10-07", "1997-10-14", "1997-10-21", "1997-10-28", "1997-11-04",
    )


def test_weekly_on_tuesday_and_thursday_for_five_weeks():
    want = days("1997-09-02", "1997-09-04", "1997-09-09", "1997-09-11", "1997-09-16",
                "1997-09-18", "1997-09-23", "1997-09-25", "1997-09-30", "1997-10-02")
    assert dts(S2, "FREQ=WEEKLY;COUNT=10;WKST=SU;BYDAY=TU,TH") == want
    assert dts(S2, "FREQ=WEEKLY;UNTIL=19971007T000000Z;WKST=SU;BYDAY=TU,TH", until=datetime(1997, 10, 7)) == want


def test_every_other_week_mo_we_fr_until_dec_24():
    got = dts(d("1997-09-01"), "FREQ=WEEKLY;INTERVAL=2;UNTIL=19971224T000000Z;WKST=SU;BYDAY=MO,WE,FR",
              until=datetime(1997, 12, 24))
    assert got == days(
        "1997-09-01", "1997-09-03", "1997-09-05", "1997-09-15", "1997-09-17", "1997-09-19",
        "1997-09-29", "1997-10-01", "1997-10-03", "1997-10-13", "1997-10-15", "1997-10-17",
        "1997-10-27", "1997-10-29", "1997-10-31", "1997-11-10", "1997-11-12", "1997-11-14",
        "1997-11-24", "1997-11-26", "1997-11-28", "1997-12-08", "1997-12-10", "1997-12-12",
        "1997-12-22",
    )


def test_every_other_week_tu_th_for_8():
    assert dts(S2, "FREQ=WEEKLY;INTERVAL=2;COUNT=8;WKST=SU;BYDAY=TU,TH") == days(
        "1997-09-02", "1997-09-04", "1997-09-16", "1997-09-18", "1997-09-30", "1997-10-02",
        "1997-10-14", "1997-10-16",
    )


def test_monthly_first_friday():
    s = d("1997-09-05")
    assert dts(s, "FREQ=MONTHLY;COUNT=10;BYDAY=1FR") == days(
        "1997-09-05", "1997-10-03", "1997-11-07", "1997-12-05", "1998-01-02",
        "1998-02-06", "1998-03-06", "1998-04-03", "1998-05-01", "1998-06-05",
    )
    got = dts(s, "FREQ=MONTHLY;UNTIL=19971224T000000Z;BYDAY=1FR", until=datetime(1997, 12, 24))
    assert got == days("1997-09-05", "1997-10-03", "1997-11-07", "1997-12-05")


def test_every_other_month_first_and_last_sunday():
    assert dts(d("1997-09-07"), "FREQ=MONTHLY;INTERVAL=2;COUNT=10;BYDAY=1SU,-1SU") == days(
        "1997-09-07", "1997-09-28", "1997-11-02", "1997-11-30", "1998-01-04", "1998-01-25",
        "1998-03-01", "1998-03-29", "1998-05-03", "1998-05-31",
    )


def test_second_to_last_monday_for_6_months():
    assert dts(d("1997-09-22"), "FREQ=MONTHLY;COUNT=6;BYDAY=-2MO") == days(
        "1997-09-22", "1997-10-20", "1997-11-17", "1997-12-22", "1998-01-19", "1998-02-16"
    )


def test_third_to_last_day_of_month():
    assert dts(d("1997-09-28"), "FREQ=MONTHLY;BYMONTHDAY=-3", hi=d("1998-02-28")) == days(
        "1997-09-28", "1997-10-29", "1997-11-28", "1997-12-29", "1998-01-29", "1998-02-26"
    )


def test_second_and_fifteenth():
    assert dts(S2, "FREQ=MONTHLY;COUNT=10;BYMONTHDAY=2,15") == days(
        "1997-09-02", "1997-09-15", "1997-10-02", "1997-10-15", "1997-11-02", "1997-11-15",
        "1997-12-02", "1997-12-15", "1998-01-02", "1998-01-15",
    )


def test_first_and_last_day_of_month():
    assert dts(d("1997-09-30"), "FREQ=MONTHLY;COUNT=10;BYMONTHDAY=1,-1") == days(
        "1997-09-30", "1997-10-01", "1997-10-31", "1997-11-01", "1997-11-30", "1997-12-01",
        "1997-12-31", "1998-01-01", "1998-01-31", "1998-02-01",
    )


def test_every_18_months_days_10_to_15():
    assert dts(d("1997-09-10"), "FREQ=MONTHLY;INTERVAL=18;COUNT=10;BYMONTHDAY=10,11,12,13,14,15") == days(
        "1997-09-10", "1997-09-11", "1997-09-12", "1997-09-13", "1997-09-14", "1997-09-15",
        "1999-03-10", "1999-03-11", "1999-03-12", "1999-03-13",
    )


def test_every_tuesday_every_other_month():
    got = dts(S2, "FREQ=MONTHLY;INTERVAL=2;BYDAY=TU", hi=d("1998-03-31"))
    assert got[:9] == days("1997-09-02", "1997-09-09", "1997-09-16", "1997-09-23", "1997-09-30",
                           "1997-11-04", "1997-11-11", "1997-11-18", "1997-11-25")
    assert d("1998-01-06") in got and d("1998-03-31") in got and d("1998-02-03") not in got


def test_yearly_june_and_july():
    assert dts(d("1997-06-10"), "FREQ=YEARLY;COUNT=10;BYMONTH=6,7") == days(
        "1997-06-10", "1997-07-10", "1998-06-10", "1998-07-10", "1999-06-10", "1999-07-10",
        "2000-06-10", "2000-07-10", "2001-06-10", "2001-07-10",
    )


def test_every_other_year_jan_feb_mar():
    assert dts(d("1997-03-10"), "FREQ=YEARLY;INTERVAL=2;COUNT=10;BYMONTH=1,2,3") == days(
        "1997-03-10", "1999-01-10", "1999-02-10", "1999-03-10", "2001-01-10", "2001-02-10",
        "2001-03-10", "2003-01-10", "2003-02-10", "2003-03-10",
    )


def test_twentieth_monday_of_the_year():
    assert dts(d("1997-05-19"), "FREQ=YEARLY;BYDAY=20MO", hi=d("1999-12-31")) == days(
        "1997-05-19", "1998-05-18", "1999-05-17"
    )


def test_every_thursday_in_march():
    assert dts(d("1997-03-13"), "FREQ=YEARLY;BYMONTH=3;BYDAY=TH", hi=d("1999-12-31")) == days(
        "1997-03-13", "1997-03-20", "1997-03-27", "1998-03-05", "1998-03-12", "1998-03-19",
        "1998-03-26", "1999-03-04", "1999-03-11", "1999-03-18", "1999-03-25",
    )


def test_every_thursday_in_summer():
    got = dts(d("1997-06-05"), "FREQ=YEARLY;BYDAY=TH;BYMONTH=6,7,8", hi=d("1997-12-31"))
    assert got == days(
        "1997-06-05", "1997-06-12", "1997-06-19", "1997-06-26", "1997-07-03", "1997-07-10",
        "1997-07-17", "1997-07-24", "1997-07-31", "1997-08-07", "1997-08-14", "1997-08-21",
        "1997-08-28",
    )


def test_friday_the_13th():
    got = [x for x in dts(S2, "FREQ=MONTHLY;BYDAY=FR;BYMONTHDAY=13", hi=d("2000-12-31")) if x != S2]
    assert got == days("1998-02-13", "1998-03-13", "1998-11-13", "1999-08-13", "2000-10-13")


def test_first_saturday_after_first_sunday():
    assert dts(d("1997-09-13"), "FREQ=MONTHLY;BYDAY=SA;BYMONTHDAY=7,8,9,10,11,12,13;COUNT=10") == days(
        "1997-09-13", "1997-10-11", "1997-11-08", "1997-12-13", "1998-01-10", "1998-02-07",
        "1998-03-07", "1998-04-11", "1998-05-09", "1998-06-13",
    )


def test_us_presidential_election_day():
    got = dts(d("1996-11-05"), "FREQ=YEARLY;INTERVAL=4;BYMONTH=11;BYDAY=TU;BYMONTHDAY=2,3,4,5,6,7,8",
              hi=d("2004-12-31"))
    assert got == days("1996-11-05", "2000-11-07", "2004-11-02")


def test_third_weekday_of_tue_wed_thu_with_setpos():
    assert dts(d("1997-09-04"), "FREQ=MONTHLY;COUNT=3;BYDAY=TU,WE,TH;BYSETPOS=3") == days(
        "1997-09-04", "1997-10-07", "1997-11-06"
    )


def test_second_to_last_weekday_of_the_month():
    got = dts(d("1997-09-29"), "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2", hi=d("1998-03-31"))
    assert got == days("1997-09-29", "1997-10-30", "1997-11-27", "1997-12-30",
                       "1998-01-29", "1998-02-26", "1998-03-30")


def test_wkst_changes_the_result():
    assert dts(d("1997-08-05"), "FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO") == days(
        "1997-08-05", "1997-08-10", "1997-08-19", "1997-08-24"
    )
    assert dts(d("1997-08-05"), "FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=SU") == days(
        "1997-08-05", "1997-08-17", "1997-08-19", "1997-08-31"
    )


def test_invalid_dates_are_skipped():
    assert dts(datetime(2007, 1, 15, 9), "FREQ=MONTHLY;BYMONTHDAY=15,30;COUNT=5") == [
        datetime(2007, 1, 15, 9), datetime(2007, 1, 30, 9), datetime(2007, 2, 15, 9),
        datetime(2007, 3, 15, 9), datetime(2007, 3, 30, 9),
    ]


def test_monthly_on_the_31st_skips_short_months():
    got = dts(datetime(2026, 1, 31, 8), "FREQ=MONTHLY;COUNT=5")
    assert [x.date() for x in got] == [date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31),
                                       date(2026, 7, 31), date(2026, 8, 31)]


def test_yearly_feb_29():
    assert dts(date(2024, 2, 29), "FREQ=YEARLY;COUNT=3") == [
        date(2024, 2, 29), date(2028, 2, 29), date(2032, 2, 29)
    ]


def test_all_day_dates_stay_dates():
    assert dts(date(2026, 3, 1), "FREQ=WEEKLY;COUNT=3") == [
        date(2026, 3, 1), date(2026, 3, 8), date(2026, 3, 15)
    ]


def test_window_skipping_matches_full_walk():
    rule = "FREQ=WEEKLY;INTERVAL=3;BYDAY=MO,FR"
    start = datetime(2020, 1, 3, 9)
    full = dts(start, rule, hi=datetime(2030, 1, 1))
    lo, hi = datetime(2026, 6, 1), datetime(2026, 9, 1)
    assert dts(start, rule, lo=lo, hi=hi) == [x for x in full if lo <= x <= hi]


@pytest.mark.parametrize(
    "rule,msg",
    [
        ("FREQ=HOURLY", "not supported"),
        ("FREQ=YEARLY;BYYEARDAY=1,100,200", "BYYEARDAY"),
        ("FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO", "BYWEEKNO"),
        ("FREQ=DAILY;BYHOUR=9", "BYHOUR"),
        ("FREQ=WEEKLY;BYDAY=2MO", "ordinals"),
        ("FREQ=MONTHLY;BYDAY=0MO", "ordinal"),
        ("FREQ=MONTHLY;BYMONTHDAY=0", "out of range"),
        ("FREQ=MONTHLY;BYMONTH=13", "out of range"),
        ("FREQ=MONTHLY;INTERVAL=0", "out of range"),
        ("FREQ=MONTHLY;COUNT=abc", "not an integer"),
        ("COUNT=3", "no FREQ"),
        ("FREQ=DAILY;FOO=1", "unknown"),
        ("FREQ=DAILY;FREQ=WEEKLY", "repeats"),
        ("FREQ=WEEKLY;WKST=XX", "WKST"),
        ("FREQ=MONTHLY;BYSETPOS=1", "BYSETPOS"),
    ],
)
def test_unsupported_or_invalid_rules_raise(rule, msg):
    with pytest.raises(ICalError, match=msg):
        parse_rule(rule)


def test_rrule_prefix_and_case_are_tolerated():
    assert parse_rule("RRULE:freq=weekly;byday=mo").byday == ((0, 0),)


# --- oracle: python-dateutil -------------------------------------------------------

pytest.importorskip("dateutil.rrule")


def _oracle(rule_text, start, hi):
    from dateutil.rrule import rrulestr

    return list(rrulestr(rule_text, dtstart=start).between(start - timedelta(seconds=1), hi, inc=True))


def _random_rule(rng):
    freq = rng.choice(["DAILY", "WEEKLY", "MONTHLY", "YEARLY"])
    parts = [f"FREQ={freq}"]
    if rng.random() < 0.5:
        parts.append(f"INTERVAL={rng.randint(1, 4)}")
    wd = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    if freq == "WEEKLY":
        if rng.random() < 0.8:
            parts.append("BYDAY=" + ",".join(rng.sample(wd, rng.randint(1, 4))))
        if rng.random() < 0.5:
            parts.append(f"WKST={rng.choice(wd)}")
    elif freq == "MONTHLY":
        k = rng.random()
        if k < 0.35:
            ords = [rng.choice([-2, -1, 1, 2, 3, 4]) for _ in range(rng.randint(1, 2))]
            parts.append("BYDAY=" + ",".join(f"{o}{rng.choice(wd)}" for o in ords))
        elif k < 0.7:
            md = [str(rng.choice([-5, -1, 1, 7, 15, 29, 30, 31])) for _ in range(rng.randint(1, 3))]
            parts.append("BYMONTHDAY=" + ",".join(md))
        elif k < 0.85:
            parts.append("BYDAY=" + ",".join(rng.sample(wd, 3)))
            parts.append(f"BYSETPOS={rng.choice([-2, -1, 1, 2])}")
    elif freq == "YEARLY":
        if rng.random() < 0.7:
            months = sorted(rng.sample(range(1, 13), rng.randint(1, 3)))
            parts.append("BYMONTH=" + ",".join(str(m) for m in months))
        k = rng.random()
        if k < 0.3:
            parts.append(f"BYDAY={rng.choice([-1, 1, 2, 3])}{rng.choice(wd)}")
        elif k < 0.5:
            parts.append(f"BYMONTHDAY={rng.choice([1, 15, 31, -1])}")
    if freq == "DAILY" and rng.random() < 0.3:
        parts.append("BYDAY=" + ",".join(rng.sample(wd, rng.randint(2, 5))))
    if freq == "DAILY" and rng.random() < 0.2:
        parts.append("BYMONTH=" + str(rng.randint(1, 12)))
    return ";".join(parts)


def test_matches_dateutil_on_random_rules():
    rng = random.Random(5545)
    for _ in range(600):
        text = _random_rule(rng)
        start = datetime(rng.randint(2019, 2027), rng.randint(1, 12), rng.randint(1, 28),
                         rng.randint(0, 23), 30)
        hi = start + timedelta(days=rng.choice([400, 1500, 4000]))
        mine = dts(start, text, hi=hi)
        theirs = _oracle(text, start, hi)
        # RFC: DTSTART is always the first instance; dateutil only if it matches the rule.
        if mine and mine[0] == start and (not theirs or theirs[0] != start):
            mine = mine[1:]
        assert mine == theirs, (text, start)


def test_matches_dateutil_with_count_when_dtstart_matches():
    from dateutil.rrule import rrulestr

    rng = random.Random(7)
    n = 0
    for _ in range(400):
        text = _random_rule(rng) + f";COUNT={rng.randint(1, 25)}"
        start = datetime(rng.randint(2019, 2027), rng.randint(1, 12), rng.randint(1, 28), 9)
        theirs = list(rrulestr(text, dtstart=start))
        if not theirs or theirs[0] != start:
            continue
        assert dts(start, text) == theirs, (text, start)
        n += 1
    assert n > 40
