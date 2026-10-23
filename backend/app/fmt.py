"""Indian number formatting: ₹1.35 Cr, ₹76.12 L, 1,97,934."""

from __future__ import annotations


def indian_int(n: float) -> str:
    neg = n < 0
    s = str(int(round(abs(n))))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if neg else "") + s


def money(v: float | None) -> str:
    if v is None:
        return "no data"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:,.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:,.2f} L"
    return f"{sign}₹{indian_int(a)}"


def count(v: float | None) -> str:
    if v is None:
        return "no data"
    return indian_int(v)


def number(v: float | None) -> str:
    if v is None:
        return "no data"
    return f"{v:,.1f}" if abs(v) < 1000 else indian_int(v)


def percent(v: float | None) -> str:
    if v is None:
        return "no data"
    return f"{v:.2f}%"


def clock(seconds_of_day: float | None) -> str:
    if seconds_of_day is None:
        return "no data"
    s = int(round(seconds_of_day)) % 86400
    h, m = divmod(s // 60, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def value(v: float | None, unit: str) -> str:
    return {"currency": money, "count": count, "percent": percent, "time": clock}.get(unit, number)(v)


def change(new: float | None, old: float | None) -> str | None:
    if new is None or old in (None, 0):
        return None
    pct = (new - old) / abs(old) * 100
    return f"{'up' if pct >= 0 else 'down'} {abs(pct):.1f}%"
