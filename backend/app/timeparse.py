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
    if token == "fortnight":
        return 1
    if token.isdigit():
        return int(token)
    return WORD_NUMBERS.get(token)


_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+days?\b"), "last_n_days"),
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+months?\b"), "last_n_months"),
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+weeks?\b"), "last_n_weeks"),
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+quarters?\b"), "last_n_quarters"),
    (re.compile(r"\b(last|past|previous)\s+(\w+)\s+years?\b"), "last_n_years"),
    (re.compile(r"\b(last|past|previous)\s+(fortnight)\b"), "fortnight"),
    (re.compile(r"\bl(\d+)m\b"), "lnm"),
]

_SIMPLE: list[tuple[str, str]] = [
    (r"\bmonth to date\b|\bmtd\b", "mtd"),
    (r"\byear to date\b|\bytd\b|\bfytd\b|\b(this|current) (financial |fiscal )?year\b", "ytd"),
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
    found = find_period_span(text, today, fy_start_month)
    return found[0] if found else None


def find_period_span(text: str, today: date, fy_start_month: int = 4) -> tuple[Period, tuple[int, int]] | None:
    """The period in the text and where it was written, so callers can remove that phrase.

    Understands exact dates and ranges ("23rd October 2026", "1-15 Oct", "from 1/9 to 15/9",
    "between Aug and Oct"), weeks ("week of 13 Oct", "second week of September"), months,
    quarters and financial years ("Sep 2026", "Q2", "FY26"), anchored durations ("10 days from
    5 Oct", "first 7 days of Oct") and the relative phrases (MTD, last 15 days, yesterday...).
    """
    t = text.lower()
    hit = _explicit_period(t, today, fy_start_month)
    if hit:
        return hit
    for pattern, kind in _PATTERNS:
        m = pattern.search(t)
        if not m:
            continue
        if kind == "lnm":
            return preset("last_n_months", today, fy_start_month, int(m.group(1))), m.span()
        n = _num(m.group(2))
        if not n:
            continue
        if kind == "fortnight":
            return preset("last_n_days", today, fy_start_month, 14), m.span()
        if kind == "last_n_weeks":
            return preset("last_n_days", today, fy_start_month, n * 7), m.span()
        if kind == "last_n_quarters":
            s = add_months(fiscal_quarter_start(today, fy_start_month), -3 * n)
            e = fiscal_quarter_start(today, fy_start_month) - timedelta(days=1)
            return Period(s, e, f"Last {n} quarters ({fmt(s)} to {fmt(e)})", "custom"), m.span()
        if kind == "last_n_years":
            s = date(today.year - n, today.month, today.day) + timedelta(days=1)
            return Period(s, today, f"Last {n} year{'s' if n > 1 else ''} ({fmt(s)} to {fmt(today)})", "custom"), m.span()
        return preset(kind, today, fy_start_month, n), m.span()
    for pattern, kind in _SIMPLE:
        m = re.search(pattern, t)
        if m:
            return preset(kind, today, fy_start_month), m.span()
    return None


# --------------------------------------------------------------------------- exact dates

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": -1, "final": -1}

_MON = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?")
_DAY = r"(?:[12]\d|3[01]|0?[1-9])(?:st|nd|rd|th)?"
_YR = r"(?:\d{4}|'\d{2})"
# One calendar day, written in any of the usual ways.
_DATE = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}"                                          # 2026-10-23
    r"|\d{1,2}[/-]\d{1,2}(?:[/-](?:\d{4}|\d{2}))?(?![\d/-]*\d)"         # 23/10/2026, 23-10-26, 23/10
    r"|\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})(?!\d)"                            # 23.10.2026 (not 2.5)
    rf"|{_DAY}(?:\s+of)?[\s-]+{_MON}(?:[\s,-]+{_YR}|-\d{{2}}(?!\d)|\s+\d{{2}}(?![\d:]))?"  # 23rd October, 2026
    rf"|{_MON}\s+{_DAY}(?!\d)(?:,?\s*{_YR})?"                              # October 23rd, 2026
    r"|day before yesterday|yesterday|today"
    r"|(?:\d+|one|two|three|four|five|six|seven|ten) days? ago"
    r"|(?:last|this|on|previous)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))"
)
# A whole month: "October 2026", "Oct'26", "in September".
_MONTH = rf"(?:(?:the\s+)?month\s+of\s+)?{_MON}(?:[\s,-]*{_YR})?"
_CONNECT = r"\s*(?:to|till|until|up\s*to|upto|through|thru|and|-|~)\s*"
_END_WORDS = r"(?:today|now|date|till date|to date)"


def _year(token: str | None, month: int, day: int, today: date) -> int:
    if token:
        token = token.strip(" ,'-")
        y = int(token)
        return y + 2000 if y < 100 else y
    # No year: the most recent such date that isn't in the future.
    y = today.year
    try:
        if date(y, month, min(day, 28)) > today and (month, day) > (today.month, today.day):
            y -= 1
    except ValueError:
        pass
    return y


def _parse_date(s: str, today: date) -> date | None:
    s = s.strip().rstrip(".,")
    if s == "today":
        return today
    if s == "yesterday":
        return today - timedelta(days=1)
    if s == "day before yesterday":
        return today - timedelta(days=2)
    m = re.fullmatch(r"(\w+) days? ago", s)
    if m:
        n = _num(m.group(1))
        return today - timedelta(days=n) if n is not None else None
    m = re.fullmatch(r"(last|this|on|previous)\s+(\w+day)", s)
    if m and m.group(2) in WEEKDAYS:
        back = (today.weekday() - WEEKDAYS[m.group(2)]) % 7
        if m.group(1) in ("last", "previous") and back == 0:
            back = 7
        return today - timedelta(days=back)
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}|\d{2}))?", s)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            if mo > 12 and d <= 12:  # written month first
                d, mo = mo, d
            return date(_year(m.group(3), mo, d, today), mo, d)
        m = re.fullmatch(rf"({_DAY})(?:\s+of)?[\s-]+({_MON})(?:[\s,-]+({_YR}|\d{{2}}))?", s)
        if m:
            d = int(re.match(r"\d+", m.group(1)).group())
            mo = MONTHS[m.group(2).rstrip(".")]
            return date(_year(m.group(3), mo, d, today), mo, d)
        m = re.fullmatch(rf"({_MON})\s+({_DAY})(?:,?\s*({_YR}))?", s)
        if m:
            d = int(re.match(r"\d+", m.group(2)).group())
            mo = MONTHS[m.group(1).rstrip(".")]
            return date(_year(m.group(3), mo, d, today), mo, d)
    except ValueError:  # 31 Feb and friends
        return None
    return None


def _parse_month(s: str, today: date) -> tuple[date, date] | None:
    m = re.fullmatch(rf"(?:(?:the\s+)?month\s+of\s+)?({_MON})(?:[\s,-]*({_YR}))?", s.strip().rstrip(".,"))
    if not m:
        return None
    mo = MONTHS[m.group(1).rstrip(".")]
    y = _year(m.group(2), mo, 1, today)
    first = date(y, mo, 1)
    return first, month_end(first)


def _label(s: date, e: date) -> str:
    if s == e:
        return fmt(s)
    if s.day == 1 and e == month_end(s):
        return s.strftime("%b %Y")
    return f"{fmt(s)} to {fmt(e)}"


def _make(s: date, e: date, today: date, label: str | None = None) -> Period:
    if s > e:
        s, e = e, s
    capped = min(e, today) if s <= today else e
    text = label or _label(s, e)
    if capped < e:
        text += f" (to {fmt(capped)})"
    return Period(s, capped, text, "custom")


def _quarter(n: int, fy_label: str | None, today: date, fy_start_month: int) -> tuple[date, date]:
    if fy_label:
        end_year = int(fy_label[-2:]) + 2000 if len(fy_label) <= 2 or fy_label.isdigit() and len(fy_label) != 4 else int(fy_label)
        fy_start = date(end_year - 1 if fy_start_month > 1 else end_year, fy_start_month, 1)
    else:
        fy_start = fiscal_year_start(today, fy_start_month)
    s = add_months(fy_start, 3 * (n - 1))
    if not fy_label and s > today:
        s = add_months(s, -12)
    return s, add_months(s, 3) - timedelta(days=1)


def _explicit_period(t: str, today: date, fy: int) -> tuple[Period, tuple[int, int]] | None:
    # "1-15 Oct", "1st to 15th October 2026", "Oct 1-15"
    m = re.search(rf"\b({_DAY}){_CONNECT}({_DAY})(?:\s+of)?\s+({_MON})(?:[\s,-]+({_YR}))?", t) or \
        re.search(rf"\b({_MON})\s+({_DAY}){_CONNECT}({_DAY})(?:,?\s*({_YR}))?", t)
    if m:
        g = m.groups()
        if re.fullmatch(_MON, g[0]):
            mon, d1, d2, yr = g[0], g[1], g[2], g[3]
        else:
            d1, d2, mon, yr = g
        mo = MONTHS[mon.rstrip(".")]
        a, b = (int(re.match(r"\d+", x).group()) for x in (d1, d2))
        try:
            y = _year(yr, mo, max(a, b), today)
            return _make(date(y, mo, a), date(y, mo, b), today), m.span()
        except ValueError:
            pass

    # "10 days from 5 Oct", "2 weeks starting 1 Sep", "7 days ending 20 Oct"
    m = re.search(rf"\b(\d+|\w+)\s+(days?|weeks?)\s+(from|starting(?:\s+from)?|beginning|after|since|before|ending(?:\s+on)?|till|until|up\s*to)\s+({_DATE})", t)
    if m and _num(m.group(1)):
        n = _num(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        anchor = _parse_date(m.group(4), today)
        if anchor:
            if m.group(3).startswith(("before", "ending", "till", "until", "up")):
                return _make(anchor - timedelta(days=n - 1), anchor, today), m.span()
            return _make(anchor, anchor + timedelta(days=n - 1), today), m.span()

    # "first 10 days of October", "last 7 days of Sep"
    m = re.search(rf"\b(first|last)\s+(\d+|\w+)\s+days\s+of\s+({_MONTH})", t)
    if m and _num(m.group(2)):
        rng = _parse_month(m.group(3), today)
        if rng:
            n = _num(m.group(2))
            s, e = (rng[0], rng[0] + timedelta(days=n - 1)) if m.group(1) == "first" else (rng[1] - timedelta(days=n - 1), rng[1])
            return _make(s, e, today), m.span()

    # "second week of September", "last week of Oct"
    m = re.search(rf"\b(first|1st|second|2nd|third|3rd|fourth|4th|last|final)\s+week\s+of\s+({_MONTH})", t)
    if m:
        rng = _parse_month(m.group(2), today)
        if rng:
            k = ORDINALS[m.group(1)]
            s = rng[1] - timedelta(days=6) if k == -1 else rng[0] + timedelta(days=7 * (k - 1))
            e = rng[1] if k == -1 else min(s + timedelta(days=6), rng[1])
            return _make(s, e, today), m.span()

    # "first half of October", "second half of Sep"
    m = re.search(rf"\b(first|1st|second|2nd)\s+half\s+of\s+({_MONTH})", t)
    if m:
        rng = _parse_month(m.group(2), today)
        if rng:
            first = m.group(1) in ("first", "1st")
            s, e = (rng[0], rng[0].replace(day=15)) if first else (rng[0].replace(day=16), rng[1])
            return _make(s, e, today), m.span()

    # "week of 13 Oct", "week starting 13/10"
    m = re.search(rf"\bweek\s+(?:of|starting|beginning|from|commencing)\s+(?:on\s+)?({_DATE})", t)
    if m:
        d = _parse_date(m.group(1), today)
        if d:
            s = d - timedelta(days=d.weekday())
            return _make(s, s + timedelta(days=6), today, f"Week of {fmt(s)}"), m.span()

    # Ranges: "from 1 Sep to 15 Sep", "between Aug and Oct 2026", "23/10 - 25/10", "1 Oct till date"
    atom = rf"(?:{_DATE}|{_MONTH})"
    m = re.search(rf"\b(?:from|between|during|for)?\s*({atom}){_CONNECT}({atom}|{_END_WORDS})(?![a-z])", t)
    if m:
        a_txt, b_txt = m.group(1), m.group(2)
        a = _parse_date(a_txt, today)
        a_rng = (a, a) if a else _parse_month(a_txt, today)
        if b_txt.strip() in ("today", "now", "date", "till date", "to date"):
            b_rng = (today, today)
        else:
            b = _parse_date(b_txt, today)
            b_rng = (b, b) if b else _parse_month(b_txt, today)
        if a_rng and b_rng:
            s, e = a_rng[0], b_rng[1]
            # "Nov to Feb", or a year written only on the end: keep the start before the end
            if s > e and not re.search(r"\d{4}|'\d{2}", a_txt):
                try:
                    s = s.replace(year=s.year - 1)
                except ValueError:
                    s = s - timedelta(days=365)
            if s <= e:
                return _make(s, e, today), m.span()

    # "since 1 Sep", "from 15th Oct" (no end) -> up to today
    m = re.search(rf"\b(?:since|from|after|starting(?:\s+from)?)\s+({atom})(?![a-z])", t)
    if m:
        a = _parse_date(m.group(1), today)
        rng = (a, a) if a else _parse_month(m.group(1), today)
        if rng and rng[0] <= today:
            return _make(rng[0], today, today), m.span()

    # Quarters and financial years: "Q2", "Q1 FY26", "FY 2025-26", "financial year 2025"
    m = re.search(r"\bq([1-4])(?:\s*(?:of\s+)?(?:fy|financial year)\s*'?(\d{2,4})(?:\s*-\s*\d{2,4})?)?\b", t)
    if m:
        s, e = _quarter(int(m.group(1)), m.group(2), today, fy)
        return _make(s, e, today, f"Q{m.group(1)} ({fmt(s)} to {fmt(e)})"), m.span()
    m = re.search(r"\b(?:fy|financial year|fiscal year|f\.y\.)\s*'?(\d{2,4})(?:\s*-\s*(\d{2,4}))?\b", t)
    if m:
        last = m.group(2) or m.group(1)
        end_year = int(last) + 2000 if len(last) == 2 else int(last)
        if not m.group(2) and len(m.group(1)) == 4 and m.group(1) != last:
            end_year = int(m.group(1))
        if m.group(2) is None and len(m.group(1)) == 4:
            end_year = int(m.group(1)) + (1 if fy > 1 else 0)  # "FY 2025" = year starting in 2025
        s = date(end_year - 1 if fy > 1 else end_year, fy, 1)
        e = add_months(s, 12) - timedelta(days=1)
        return _make(s, e, today, f"FY {s.year}-{str(e.year)[-2:]} ({fmt(s)} to {fmt(e)})"), m.span()

    # One day: "on 23rd October", "23/10/2026", "last Monday", "3 days ago"
    m = re.search(rf"(?<![\w/.-])({_DATE})(?![\w/])", t)
    if m:
        d = _parse_date(m.group(1), today)
        if d:
            text = m.group(1).strip()
            if text in ("today", "yesterday"):
                return preset(text, today, fy), m.span()
            return _make(d, d, today), m.span()

    # A whole month: "in September", "October 2026", "Oct'26". "may" needs a year or "in/for/of".
    m = re.search(rf"\b(?:(?:in|for|of|during)\s+)?({_MONTH})(?![a-z])", t)
    if m:
        txt = m.group(1)
        if not (txt.strip().startswith("may") and not re.search(r"\d", txt) and not re.match(r"(in|for|of|during)\s", m.group(0))):
            rng = _parse_month(txt, today)
            if rng:
                return _make(rng[0], rng[1], today), m.span()

    # A calendar year: "in 2025", "for 2026", "calendar year 2025"
    m = re.search(r"\b(?:in|for|during|calendar year|year)\s+(20\d{2})\b", t)
    if m:
        y = int(m.group(1))
        return _make(date(y, 1, 1), date(y, 12, 31), today, f"Year {y}"), m.span()
    return None


def grain_for(p: Period) -> str:
    if p.days <= 31:
        return "day"
    if p.days <= 92:
        return "week"
    return "month"
