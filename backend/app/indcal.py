"""Indian selling calendar: working days, holidays, festivals and seasons for a period.

Reads backend/data/india_calendar.json, which anyone can edit. Used by the RCA layer to say
how much of a change is explained by fewer working days or a known seasonal pattern.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from . import config
from .timeparse import Period, month_end


@dataclass(frozen=True)
class Holiday:
    start: date
    end: date
    name: str
    type: str  # national | festival | regional
    field_off: bool
    approx: bool = False

    def overlaps(self, p: Period) -> bool:
        return self.start <= p.end and self.end >= p.start

    @property
    def when(self) -> str:
        s = self.start.strftime("%d %b").lstrip("0")
        if self.end != self.start:
            return f"{s} to {self.end.strftime('%d %b').lstrip('0')}"
        return s


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((config.DATA_DIR / "india_calendar.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def holidays() -> tuple[Holiday, ...]:
    out = []
    for h in _data()["holidays"]:
        s = date.fromisoformat(h["date"])
        e = date.fromisoformat(h.get("end", h["date"]))
        out.append(Holiday(s, e, h["name"], h.get("type", "festival"), bool(h.get("field_off")), bool(h.get("approx"))))
    return tuple(sorted(out, key=lambda x: x.start))


def holidays_in(p: Period) -> list[Holiday]:
    return [h for h in holidays() if h.overlaps(p)]


def off_days(p: Period) -> set[date]:
    """Days in the period the field force normally doesn't work: Sundays and field-off holidays."""
    days = set()
    d = p.start
    while d <= p.end:
        if d.weekday() == 6:
            days.add(d)
        d += timedelta(days=1)
    for h in holidays_in(p):
        if h.field_off:
            d = max(h.start, p.start)
            while d <= min(h.end, p.end):
                days.add(d)
                d += timedelta(days=1)
    return days


def working_days(p: Period) -> int:
    return p.days - len(off_days(p))


def _find(name: str, near: date) -> Holiday | None:
    hits = [h for h in holidays() if h.name.lower() == name.lower()]
    return min(hits, key=lambda h: abs((h.start - near).days), default=None)


def seasons(p: Period) -> list[tuple[str, str, int]]:
    """(id, note, effect) for each seasonal pattern that touches the period; effect +1 lifts sales, -1 lowers."""
    out: list[tuple[str, str, int]] = []
    for s in _data().get("seasons", []):
        rule, hit = s["rule"], False
        if rule == "months":
            d = p.start
            while d <= p.end and not hit:
                hit = d.month in s["months"]
                d += timedelta(days=1)
        elif rule == "last_days_of_month":
            hit = any(month_end(d) - d < timedelta(days=s["days"]) for d in _days(p))
        elif rule == "first_days_of_month":
            hit = any(d.day <= s["days"] for d in _days(p))
        elif rule in ("before_holiday", "after_holiday", "during_holiday"):
            h = _find(s["holiday"], p.start)
            if h:
                if rule == "before_holiday":
                    win = (h.start - timedelta(days=s["days"]), h.start - timedelta(days=1))
                elif rule == "after_holiday":
                    win = (h.end + timedelta(days=1), h.end + timedelta(days=s["days"]))
                else:
                    win = (h.start, h.end)
                hit = win[0] <= p.end and win[1] >= p.start
        if hit:
            out.append((s["id"], s["note"], int(s.get("effect", 0))))
    return out


def _days(p: Period):
    d = p.start
    while d <= p.end:
        yield d
        d += timedelta(days=1)
