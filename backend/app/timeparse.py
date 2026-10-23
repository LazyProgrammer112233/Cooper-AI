"""Turns time phrases ("MTD", "last 15 days", "last quarter") into date ranges.

All dates are in the company's local time zone, and quarters / years follow the
company's financial year (1 April for most Indian companies).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "thirty": 30, "a": 1,
}


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str
    preset: str = ""

    @property
    def start_key(self) -> int:
        return int(self.start.strftime("%Y%m%d"))

    @property
    def end_key(self) -> int:
        return int(self.end.strftime("%Y%m%d"))

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label, "preset": self.preset}


def company_today(tz_offset_minutes: int, now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return (now.astimezone(timezone.utc) + timedelta(minutes=tz_offset_minutes)).date()


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def month_end(d: date) -> date:
    return add_months(d, 1) - timedelta(days=1)


def fiscal_year_start(d: date, fy_start_month: int) -> date:
    year = d.year if d.month >= fy_start_month else d.year - 1
    return date(year, fy_start_month, 1)


def fiscal_quarter_start(d: date, fy_start_month: int) -> date:
    offset = (d.month - fy_start_month) % 12
    return add_months(date(d.year, d.month, 1), -(offset % 3))


def fmt(d: date) -> str:
    return d.strftime("%d %b %Y").lstrip("0")


def preset(name: str, today: date, fy_start_month: int = 4, n: int = 0) -> Period:
    if name == "today":
        return Period(today, today, f"Today ({fmt(today)})", name)
    if name == "yesterday":
        y = today - timedelta(days=1)
        return Period(y, y, f"Yesterday ({fmt(y)})", name)
    if name == "this_week":
        s = today - timedelta(days=today.weekday())
        return Period(s, today, f"This week ({fmt(s)} to {fmt(today)})", name)
    if name == "last_week":
        s = today - timedelta(days=today.weekday() + 7)
        e = s + timedelta(days=6)
        return Period(s, e, f"Last week ({fmt(s)} to {fmt(e)})", name)
    if name == "last_n_days":
        s = today - timedelta(days=n - 1)
        return Period(s, today, f"Last {n} days ({fmt(s)} to {fmt(today)})", name)
    if name == "mtd":
        s = month_start(today)
        return Period(s, today, f"MTD ({fmt(s)} to {fmt(today)})", name)
    if name == "last_month":
        s = add_months(month_start(today), -1)
        e = month_end(s)
        return Period(s, e, f"Last month ({s.strftime('%b %Y')})", name)
    if name == "lmtd":
        s = add_months(month_start(today), -1)
        e = min(s.replace(day=min(today.day, month_end(s).day)), month_end(s))
        return Period(s, e, f"LMTD ({fmt(s)} to {fmt(e)})", name)
    if name == "last_n_months":
        s = add_months(month_start(today), -(n - 1))
        return Period(s, today, f"Last {n} months ({s.strftime('%b %Y')} to {fmt(today)})", name)
    if name == "qtd":
        s = fiscal_quarter_start(today, fy_start_month)
        return Period(s, today, f"This quarter ({fmt(s)} to {fmt(today)})", name)
    if name == "last_quarter":
        s = add_months(fiscal_quarter_start(today, fy_start_month), -3)
        e = add_months(s, 3) - timedelta(days=1)
        return Period(s, e, f"Last quarter ({fmt(s)} to {fmt(e)})", name)
    if name == "ytd":
        s = fiscal_year_start(today, fy_start_month)
        return Period(s, today, f"YTD, financial year ({fmt(s)} to {fmt(today)})", name)
    if name == "last_year":
        s = date(fiscal_year_start(today, fy_start_month).year - 1, fy_start_month, 1)
        e = fiscal_year_start(today, fy_start_month) - timedelta(days=1)
        return Period(s, e, f"Last financial year ({fmt(s)} to {fmt(e)})", name)
    raise ValueError(f"Unknown period preset {name}")


def previous_period(p: Period, today: date, fy_start_month: int = 4) -> Period:
    """The period to compare against for "growth" style questions."""
    if p.preset in ("mtd", "this_month"):
        return preset("lmtd", today, fy_start_month)
    if p.preset == "last_month":
        s = add_months(p.start, -1)
        return Period(s, month_end(s), f"{s.strftime('%b %Y')}", "month")
    if p.preset == "today":
        return preset("yesterday", today, fy_start_month)
    length = p.days
    e = p.start - timedelta(days=1)
    s = e - timedelta(days=length - 1)
    return Period(s, e, f"Previous {length} days ({fmt(s)} to {fmt(e)})", "previous")


def _num(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return WORD_NUMBERS.get(token)


_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+days?\b"), "last_n_days"),
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+months?\b"), "last_n_months"),
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+weeks?\b"), "last_n_weeks"),
    (re.compile(r"\bl(\d+)m\b"), "lnm"),
]

_SIMPLE: list[tuple[str, str]] = [
    (r"\bmonth to date\b|\bmtd\b", "mtd"),
    (r"\byear to date\b|\bytd\b|\bfytd\b", "ytd"),
    (r"\bquarter to date\b|\bqtd\b|\bthis quarter\b|\bcurrent quarter\b", "qtd"),
    (r"\blmtd\b|\blast month to date\b", "lmtd"),
    (r"\b(last|previous|past) quarter\b", "last_quarter"),
    (r"\b(last|previous|past) (financial )?year\b", "last_year"),
    (r"\b(last|previous|past) month\b", "last_month"),
    (r"\b(this|current) month\b", "mtd"),
    (r"\b(last|previous|past) week\b", "last_week"),
    (r"\b(this|current) week\b", "this_week"),
    (r"\byesterday\b|\bd-1\b", "yesterday"),
    (r"\btoday\b|\btoday's\b|\btodays\b", "today"),
]


def find_period(text: str, today: date, fy_start_month: int = 4) -> Period | None:
    """Return the first explicit period mentioned in the text, or None."""
    t = text.lower()
    for pattern, kind in _PATTERNS:
        m = pattern.search(t)
        if not m:
            continue
        if kind == "lnm":
            n = int(m.group(1))
            return preset("last_n_months", today, fy_start_month, n)
        n = _num(m.group(2))
        if not n:
            continue
        if kind == "last_n_weeks":
            return preset("last_n_days", today, fy_start_month, n * 7)
        return preset(kind, today, fy_start_month, n)
    for pattern, kind in _SIMPLE:
        if re.search(pattern, t):
            return preset(kind, today, fy_start_month)
    return None


def grain_for(p: Period) -> str:
    if p.days <= 31:
        return "day"
    if p.days <= 92:
        return "week"
    return "month"
