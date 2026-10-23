from datetime import date

import pytest

from backend.app.interpreter import parse
from backend.app.timeparse import find_period

TODAY = date(2026, 10, 24)


@pytest.mark.parametrize("text,start,end", [
    ("show the productive calls for single day i.e 23rd october, 2026", date(2026, 10, 23), date(2026, 10, 23)),
    ("pc on 23/10/2026", date(2026, 10, 23), date(2026, 10, 23)),
    ("pc on 23.10.2026", date(2026, 10, 23), date(2026, 10, 23)),
    ("tc on 2026-10-20", date(2026, 10, 20), date(2026, 10, 20)),
    ("sales on oct 23", date(2026, 10, 23), date(2026, 10, 23)),
    ("day before yesterday", date(2026, 10, 22), date(2026, 10, 22)),
    ("3 days ago", date(2026, 10, 21), date(2026, 10, 21)),
    ("last monday", date(2026, 10, 19), date(2026, 10, 19)),
    ("sales for 1-15 oct", date(2026, 10, 1), date(2026, 10, 15)),
    ("from 1st sep to 15th sep 2026", date(2026, 9, 1), date(2026, 9, 15)),
    ("coverage from 01/09 to 30/09", date(2026, 9, 1), date(2026, 9, 30)),
    ("oct 1 - oct 15", date(2026, 10, 1), date(2026, 10, 15)),
    ("between aug and oct", date(2026, 8, 1), TODAY),
    ("nov to feb", date(2025, 11, 1), date(2026, 2, 28)),
    ("1 oct till date", date(2026, 10, 1), TODAY),
    ("ubo since 1 sep", date(2026, 9, 1), TODAY),
    ("sales in september", date(2026, 9, 1), date(2026, 9, 30)),
    ("sales for may", date(2026, 5, 1), date(2026, 5, 31)),
    ("week of 13 oct", date(2026, 10, 12), date(2026, 10, 18)),
    ("second week of september", date(2026, 9, 8), date(2026, 9, 14)),
    ("last week of sep", date(2026, 9, 24), date(2026, 9, 30)),
    ("first 10 days of october", date(2026, 10, 1), date(2026, 10, 10)),
    ("10 days from 5 oct", date(2026, 10, 5), date(2026, 10, 14)),
    ("7 days ending 20 oct", date(2026, 10, 14), date(2026, 10, 20)),
    ("sales q2", date(2026, 7, 1), date(2026, 9, 30)),
    ("q1 fy26", date(2025, 4, 1), date(2025, 6, 30)),
    ("fy 2025-26", date(2025, 4, 1), date(2026, 3, 31)),
    ("last fortnight", date(2026, 10, 11), TODAY),
    ("last 15 days", date(2026, 10, 10), TODAY),
    ("last week", date(2026, 10, 12), date(2026, 10, 18)),
])
def test_dates(text, start, end):
    p = find_period(text, TODAY)
    assert p is not None, text
    assert (p.start, p.end) == (start, end)


@pytest.mark.parametrize("text", ["may i see sales", "show sales for 2.5 crore outlets", "top 5 skus by sales"])
def test_not_dates(text):
    assert find_period(text, TODAY) is None


def test_single_day_question_uses_that_day():
    plan = parse("Show the productive calls for single day i.e 23rd October, 2026", TODAY)
    assert plan.metrics == ["pc"] and plan.shape == "value" and plan.filters == []
    assert (plan.period.start, plan.period.end) == (date(2026, 10, 23), date(2026, 10, 23))


def test_date_vs_date_compares_periods():
    plan = parse("sales 23 Oct vs 22 Oct", TODAY)
    assert plan.shape == "compare_periods"
    assert [p.start.day for p in plan.compare_periods] == [23, 22]


def test_date_range_with_place_filter():
    plan = parse("secondary sales from 1st Sep to 15th Sep for North zone", TODAY)
    assert [f.text for f in plan.filters] == ["north"]
    assert plan.period.days == 15
