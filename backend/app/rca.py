"""Root cause analysis: "why did PC fall this week?", "why is primary low?", "what are reps doing wrong?".

Every number comes from Pulse queries that pass the SQL guard. The steps:

1. Compare the asked period with the previous like-for-like period (this week so far vs the same
   weekdays last week, MTD vs LMTD, ...). Today is left out while its data is still syncing.
2. Driver tree: split the metric into multiplying drivers (PC = reps x days per rep x calls per day
   x PC %) and rank each driver by its share of the change (log decomposition, so shares add up).
3. Contribution: the zones, regions, reps, distributors and categories that moved the most.
4. Calendar: working days, holidays and festivals in either period (india_calendar.json), plus what
   the field data itself says (day-starts marked Holiday / Leave / Weekly off).
5. Actions from a fixed FMCG playbook (rca_playbook.json). The local model may only reword.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Awaitable, Callable

from . import config, fmt, indcal
from .catalog import DIM_LABELS, LOOKUPS, METRICS, pick_variant
from .guard import GuardViolation
from .mcp_client import DataError
from .sqlbuild import Filter, build_metric
from .timeparse import Period, add_months, month_end, previous_period

# --------------------------------------------------------------------------- detection

RCA_CUE = re.compile(
    r"\bwhy\b|\breasons?\b|root[- ]?cause|\brca\b|what went wrong|\bwhat'?s wrong\b|\bwrong with\b|doing wrong|"
    r"\bimprove\w*|\bsuggest\w*|\brecommend\w*|\bhow (can|do|should|to) (i|we)?\s*(increase|boost|grow|fix|lift|raise)\b|"
    r"\bdrop(ped|ping|s)?\b(?!\s*-?\s*size)|\bdeclin\w*|\bfell\b|\bfall(en|ing|s)?\b|\bdipp?(ed|ing)?\b|"
    r"\bincreas(ed|ing)\b|\bwent up\b|\bspiked?\b|\bjump(ed)?\b|\bslump\w*|\bdeteriorat\w*|"
    r"\banaly[sz]e why\b|\bwhat caused\b|\bexplain (the|this|why)\b"
)
_REP_WORDS = re.compile(r"\b(reps?|salesm[ae]n|dsrs?|field (force|team|users?)|team|isrs?|sales ?persons?|people)\b")


def is_rca(normalised_question: str) -> bool:
    return bool(RCA_CUE.search(normalised_question))


def default_metric(normalised_question: str) -> tuple[str, str]:
    """The metric to explain when the question names none, with a note saying so."""
    if _REP_WORDS.search(normalised_question):
        return "pc", "You didn't name a metric, so I looked at productive calls, the main measure of field productivity."
    return "secondary_sales", "You didn't name a metric, so I looked at secondary sales."


FAMILY = {
    **dict.fromkeys(("tc", "pc", "pc_pct", "avg_calls_per_rep", "avg_call_start_time", "lpc", "visited_outlets",
                     "productive_outlets", "orders"), "field"),
    **dict.fromkeys(("secondary_sales", "order_value", "secondary_invoice_value", "drop_size", "ubo", "ubo_pct"), "secondary"),
    **dict.fromkeys(("primary_sales", "primary_invoice_value"), "primary"),
    **dict.fromkeys(("coverage_pct", "penetration_pct"), "coverage"),
}
CONTRIB_DIMS = {
    "field": ["zone", "region", "rep", "distributor"],
    "secondary": ["zone", "region", "rep", "distributor", "category"],
    "primary": ["zone", "region", "distributor", "category"],
    "coverage": ["zone", "region", "rep"],
    "other": ["zone", "region", "category"],
}
SNAPSHOT_ONLY = {"active_outlets", "inactive_outlets"}
# Metrics that are exactly the product of the field chain, so driver shares add up to 100%
CHAIN_METRICS = {"tc", "pc", "secondary_sales", "order_value", "secondary_invoice_value"}

# --------------------------------------------------------------------------- periods


@dataclass
class Window:
    cur: Period
    prev: Period
    cur_name: str
    prev_name: str
    notes: list[str]


def _rng(p: Period) -> str:
    s, e = p.start.strftime("%d %b").lstrip("0"), p.end.strftime("%d %b").lstrip("0")
    return s if p.start == p.end else f"{s} to {e}"


def _p(s: date, e: date, preset: str = "") -> Period:
    return Period(s, e, f"{s:%d %b %Y} to {e:%d %b %Y}", preset)


def windows(p: Period, today: date) -> Window:
    """The asked period (to yesterday while today is syncing) and its like-for-like comparison."""
    notes: list[str] = []
    s, e = p.start, p.end
    if e >= today and s < today:
        e = today - timedelta(days=1)
        notes.append(f"Today's field data is still syncing, so I compared up to yesterday ({e:%d %b}).")
    names = {
        "this_week": ("this week so far", "the same days last week"),
        "last_week": ("last week", "the week before"),
        "today": ("today", "the same day last week"),
        "yesterday": ("yesterday", "the same day the week before"),
        "mtd": ("this month so far", "the same days last month"),
        "last_month": ("last month", "the month before"),
        "qtd": ("this quarter so far", "the same days last quarter"),
        "last_quarter": ("last quarter", "the quarter before"),
        "ytd": ("this financial year so far", "the same days last year"),
    }
    cur_name, prev_name = names.get(p.preset, ("this period", "the previous period"))
    if p.preset == "today":
        notes.append("Today's data is still coming in, so today's numbers will rise through the day.")
    days = (e - s).days + 1
    if p.preset in ("mtd", "qtd", "last_month", "last_quarter"):
        months = 3 if "q" in p.preset else 1
        ps = add_months(s, -months)
        if p.preset in ("last_month", "last_quarter"):
            pe = add_months(s, 0) - timedelta(days=1)
        elif p.preset == "mtd":
            pe = min(ps + timedelta(days=days - 1), month_end(ps))
        else:
            pe = ps + timedelta(days=days - 1)
        prev = _p(ps, pe)
    elif p.preset == "ytd":
        ps = date(s.year - 1, s.month, s.day)
        prev = _p(ps, ps + timedelta(days=days - 1))
    elif days <= 7:
        prev = _p(s - timedelta(days=7), e - timedelta(days=7))
        if p.preset not in names:
            prev_name = "the same days last week"
    else:
        pp = previous_period(_p(s, e), today)
        prev = _p(pp.start, pp.end)
        if p.preset not in names:
            prev_name = f"the previous {days} days"
    cur = _p(s, e, p.preset)
    return Window(cur, prev, f"{cur_name} ({_rng(cur)})", f"{prev_name} ({_rng(prev)})", notes)


# --------------------------------------------------------------------------- SQL (all checked by the guard)

EDP_DIMS = {"zone": "ZoneId", "region": "RegionId", "territory": "TerritaryId", "rep": "FieldUserId"}
PRIMARY_DIMS = {"zone": "BuyerZoneId", "region": "BuyerRegionId", "distributor": "BuyerId", "brand": "ProductDivisionId",
                "category": "PrimaryCategoryId", "sku": "ProductId"}
LATE_SECONDS = 10 * 3600 + 30 * 60  # first call after 10:30 local time


def _between(col: str, w: Window) -> str:
    return (f"(({col} BETWEEN {w.cur.start_key} AND {w.cur.end_key}) OR "
            f"({col} BETWEEN {w.prev.start_key} AND {w.prev.end_key}))")


def _filter_conds(filters: list[Filter], cols: dict[str, str]) -> tuple[list[str], list[Filter]]:
    conds, ignored = [], []
    for f in filters:
        if f.dim in cols and f.ids:
            conds.append(f"toInt64({cols[f.dim]}) IN ({', '.join(str(int(i)) for i in f.ids)})")
        else:
            ignored.append(f)
    return conds, ignored


def _edp_inner(cid: int, w: Window, filters: list[Filter], tz_offset: int) -> tuple[str, list[Filter]]:
    conds, ignored = _filter_conds(filters, EDP_DIMS)
    where = " AND ".join([f"CompanyId = {cid}", _between("DayStartDateKey", w)] + conds)
    tz = int(tz_offset) * 60
    inner = (
        f"SELECT if(DayStartDateKey >= {w.cur.start_key}, 'cur', 'prev') AS p, DayStartDateKey AS d, FieldUserId AS u, "
        f"toString(DayStartType) AS t, IsDayNotStarted AS x_ns, TotalTC AS x_tc, TotalPC AS x_pc, TotalSC AS x_sc, "
        f"TotalLinesCut AS x_lines, toFloat64(TotalSFAOrderedNetValue) AS x_val, "
        f"if(FirstCallTime IS NULL, NULL, (toUnixTimestamp(FirstCallTime) + {tz}) % 86400) AS x_fc, "
        f"TimeSpentRetailinginMinutes AS x_mins, IsTourPlanAdhered AS x_adh "
        f"FROM EmployeeDayPerformance WHERE {where}"
    )
    return inner, ignored


def edp_summary_sql(cid: int, w: Window, filters: list[Filter], tz_offset: int = 330) -> tuple[str, list[Filter]]:
    inner, ignored = _edp_inner(cid, w, filters, tz_offset)
    sql = (
        "SELECT p, uniqExactIf(u, x_tc > 0) AS reps, countIf(x_tc > 0) AS rep_days, "
        "toFloat64(sum(x_tc)) AS tc, toFloat64(sum(x_pc)) AS pc, toFloat64(sumIf(x_sc, x_tc > 0)) AS sc, "
        "toFloat64(sum(x_lines)) AS lines, sum(x_val) AS val, "
        "countIf(t = 'Holiday') AS holiday_days, countIf(t = 'Leave') AS leave_days, countIf(t = 'WeeklyOff') AS off_days, "
        "countIf(x_tc > 0 AND x_pc = 0) AS zero_pc_days, avgIf(x_fc, x_tc > 0 AND x_fc IS NOT NULL) AS first_call, "
        f"countIf(x_tc > 0 AND x_fc > {LATE_SECONDS}) AS late_days, avgIf(x_mins, x_tc > 0) AS retail_mins, "
        "countIf(x_tc > 0 AND x_adh = 1) AS adhered_days, uniqExact(d) AS days_with_data "
        f"FROM ({inner}) AS s GROUP BY p"
    )
    return sql, ignored


def edp_daily_sql(cid: int, w: Window, filters: list[Filter], tz_offset: int = 330) -> str:
    inner, _ = _edp_inner(cid, w, filters, tz_offset)
    return ("SELECT d, countIf(t = 'Holiday') AS holiday_days, countIf(t = 'Leave') AS leave_days, "
            "countIf(t = 'WeeklyOff') AS off_days, countIf(x_tc > 0) AS rep_days, toFloat64(sum(x_tc)) AS tc, "
            f"toFloat64(sum(x_pc)) AS pc FROM ({inner}) AS s GROUP BY d ORDER BY d")


def edp_reps_sql(cid: int, w: Window, filters: list[Filter], tz_offset: int = 330) -> str:
    """Per-rep field stats for the current period, with names."""
    inner, _ = _edp_inner(cid, w, filters, tz_offset)
    table, id_col, name_col, company_col = LOOKUPS["rep"]
    agg = (
        "SELECT u, countIf(x_tc > 0) AS days, toFloat64(sum(x_tc)) AS tc, toFloat64(sum(x_pc)) AS pc, "
        f"countIf(x_tc > 0 AND x_pc = 0) AS zero_pc_days, avgIf(x_fc, x_tc > 0 AND x_fc IS NOT NULL) AS first_call, "
        f"countIf(x_tc > 0 AND x_fc > {LATE_SECONDS}) AS late_days "
        f"FROM ({inner}) AS s WHERE p = 'cur' GROUP BY u HAVING tc >= 20"
    )
    return (f"SELECT a.*, n.name AS name FROM ({agg}) AS a LEFT JOIN (SELECT toInt64({id_col}) AS _id, "
            f"any({name_col}) AS name FROM {table} FINAL WHERE {company_col} = {cid} GROUP BY _id) AS n "
            f"ON toInt64(a.u) = n._id ORDER BY pc / tc ASC LIMIT 200")


def primary_sql(cid: int, w: Window, filters: list[Filter]) -> tuple[str, list[Filter]]:
    conds, ignored = _filter_conds(filters, PRIMARY_DIMS)
    where = " AND ".join([f"CompanyId = {cid}", _between("InvoiceDateKey", w),
                          "InvoiceId > 0", "IsInvoiceCancelled = 0"] + conds)
    sql = (
        f"SELECT p, uniqExact(BuyerId) AS dists, uniqExact(InvoiceId) AS invoices, sum(v) AS val FROM ("
        f"SELECT if(InvoiceDateKey >= {w.cur.start_key}, 'cur', 'prev') AS p, BuyerId, InvoiceId, "
        f"toFloat64(InvoiceNetValue) AS v FROM PrimaryReprimary WHERE {where}) AS s GROUP BY p"
    )
    return sql, ignored


# --------------------------------------------------------------------------- playbook and helpers


@lru_cache(maxsize=1)
def playbook() -> dict:
    return json.loads((config.DATA_DIR / "rca_playbook.json").read_text(encoding="utf-8"))["findings"]


def _n(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _div(a: float | None, b: float | None, scale: float = 1.0) -> float | None:
    if a is None or not b:
        return None
    return a / b * scale


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new - old) / abs(old) * 100


def _chg(new: float | None, old: float | None, unit: str) -> str:
    """'down 12.3%' / 'up 2.1 pts' / '18 min later'."""
    if new is None or old is None:
        return "no comparison"
    if unit == "percent":
        d = new - old
        return f"{'up' if d >= 0 else 'down'} {abs(d):.1f} pts"
    if unit == "time":
        d = round((new - old) / 60)
        return "same time" if d == 0 else f"{abs(d)} min {'later' if d > 0 else 'earlier'}"
    p = _pct(new, old)
    return "no comparison" if p is None else f"{'up' if p >= 0 else 'down'} {abs(p):.1f}%"


def _v(v: float | None, unit: str) -> str:
    return fmt.value(v, unit)


@dataclass
class Finding:
    id: str
    text: str
    score: float
    likely: bool = False  # True when it comes from the calendar, not from the data


# --------------------------------------------------------------------------- the analysis

Query = Callable[[Any, str, int, float], Awaitable[list[dict]]]


async def analyse(session, query: Query, *, ctx, metric_id: str, period: Period, filters: list[Filter],
                  saved: dict[str, str], question: str, budget: float, extra_notes: list[str]) -> dict:
    cid = ctx.id
    today = ctx.today
    w = windows(period, today)
    metric = METRICS[metric_id]
    family = FAMILY.get(metric_id, "other")
    unit = metric.unit
    notes = list(extra_notes) + w.notes
    sqls: list[str] = []
    filter_dims = [f.dim for f in filters]

    # ---- build every query up front, then run them all in parallel
    tasks: dict[str, str] = {}
    variant, vnote = pick_variant(metric_id, saved.get(metric_id), filter_dims)
    if vnote:
        notes.append(vnote)
    for tag, per in (("head_cur", w.cur), ("head_prev", w.prev)):
        built = build_metric(cid, variant, [], per, filters, tz_offset_minutes=ctx.tz_offset)
        tasks[tag] = built.sql
        if tag == "head_cur":
            for f in built.ignored_filters:
                notes.append(f"The {DIM_LABELS.get(f.dim, f.dim).lower()} filter ({f.label}) can't be applied to "
                             f"{metric.label.lower()}, so that figure covers everything.")
    field_side = family in ("field", "secondary", "coverage") or _REP_WORDS.search(question.lower())
    if family != "primary" or field_side:
        sql, ignored = edp_summary_sql(cid, w, filters, ctx.tz_offset)
        tasks["edp"] = sql
        tasks["daily"] = edp_daily_sql(cid, w, filters, ctx.tz_offset)
        if ignored and family != "primary":
            notes.append("Field activity (reps, calls, start times) can't be split by "
                         + ", ".join(DIM_LABELS.get(f.dim, f.dim).lower() for f in ignored)
                         + ", so the driver figures cover the whole team.")
    if field_side:
        tasks["reps"] = edp_reps_sql(cid, w, filters, ctx.tz_offset)
    if family == "primary":
        sql, _ = primary_sql(cid, w, filters)
        tasks["primary"] = sql
    extra_metrics = {"secondary": ["ubo", "drop_size"], "field": [], "coverage": [], "primary": [], "other": []}[family]
    extra_metrics = [m for m in extra_metrics if m != metric_id]
    for m in extra_metrics:
        v2, _ = pick_variant(m, saved.get(m), filter_dims)
        for tag, per in (("cur", w.cur), ("prev", w.prev)):
            tasks[f"x_{m}_{tag}"] = build_metric(cid, v2, [], per, filters, tz_offset_minutes=ctx.tz_offset).sql
    contrib_variants = {}
    for dim in CONTRIB_DIMS[family]:
        if dim in filter_dims:
            continue
        v3, _ = pick_variant(metric_id, saved.get(metric_id), [dim] + filter_dims)
        if not v3.supports(dim):
            continue
        contrib_variants[dim] = v3
        for tag, per in (("cur", w.cur), ("prev", w.prev)):
            tasks[f"c_{dim}_{tag}"] = build_metric(cid, v3, [dim], per, filters, tz_offset_minutes=ctx.tz_offset).sql

    async def one(sql: str):
        return await query(session, sql, cid, budget)

    keys = list(tasks)
    sqls = [tasks[k] for k in keys]
    results = await asyncio.gather(*[one(s) for s in sqls], return_exceptions=True)
    got: dict[str, list[dict]] = {}
    failed = 0
    for k, r in zip(keys, results):
        if isinstance(r, GuardViolation):
            raise r
        if isinstance(r, BaseException):
            failed += 1
            continue
        got[k] = r
    if "head_cur" not in got or "head_prev" not in got:
        raise DataError("the main figures didn't come back from Pulse")
    if failed:
        notes.append(f"{failed} of {len(keys)} supporting queries didn't come back in time, so a few drivers are missing.")

    # ---- headline
    h1 = (got["head_cur"] or [{}])[0]
    h0 = (got["head_prev"] or [{}])[0]
    v1, v0 = _n(h1.get("v")), _n(h0.get("v"))
    change_pct = _pct(v1, v0)
    if unit == "percent":
        delta = None if v1 is None or v0 is None else v1 - v0
        moved = delta is not None and abs(delta) >= 0.5
        sign = 0 if not moved else (1 if delta > 0 else -1)
    elif unit == "time":
        delta = None if v1 is None or v0 is None else v1 - v0
        sign = 0 if delta is None or abs(delta) < 300 else (1 if delta > 0 else -1)
    else:
        sign = 0 if change_pct is None or abs(change_pct) < 2 else (1 if change_pct > 0 else -1)
    # "worse" is a fall for most metrics, a later time for start time
    worse = sign < 0 if metric.higher_is_better else sign > 0
    direction = "flat" if sign == 0 else ("up" if sign > 0 else "down")

    # ---- calendar
    hol_cur, hol_prev = indcal.holidays_in(w.cur), indcal.holidays_in(w.prev)
    wd1, wd0 = indcal.working_days(w.cur), indcal.working_days(w.prev)
    calendar: list[str] = []
    cur_word, prev_word = w.cur_name.split(" (")[0], w.prev_name.split(" (")[0]
    for h in sorted(set(hol_cur) | set(hol_prev), key=lambda x: x.start):
        where_ = "both periods" if h in hol_cur and h in hol_prev else (cur_word if h in hol_cur else prev_word)
        tag = {"national": "national holiday", "festival": "festival", "regional": "regional festival"}.get(h.type, h.type)
        calendar.append(f"{h.name} ({h.when}{', date may vary by a day' if h.approx else ''}) is a {tag} in {where_}"
                        + (", and most field teams take it off." if h.field_off else "."))
    calendar.append(f"Working days (Mon to Sat, minus field holidays): {wd1} in {w.cur_name.split(' (')[0]} vs "
                    f"{wd0} in {w.prev_name.split(' (')[0]}.")
    seas_cur = {sid: (note, eff) for sid, note, eff in indcal.seasons(w.cur)}
    seas_prev = {sid: (note, eff) for sid, note, eff in indcal.seasons(w.prev)}
    season_notes = []  # (id, note, in_cur, explains): explains when it pushes the metric the way it moved
    for sid in set(seas_cur) ^ set(seas_prev):
        if sid in ("month_end", "month_start", "fy_end") and family != "primary":
            continue
        in_cur = sid in seas_cur
        note, eff = seas_cur.get(sid) or seas_prev[sid]
        push = eff if in_cur else -eff
        season_notes.append((sid, note, in_cur, push != 0 and push == sign))

    # ---- field drivers from the daily summaries
    e = {r.get("p"): r for r in got.get("edp", [])}
    e1, e0 = e.get("cur", {}), e.get("prev", {})
    findings: list[Finding] = []
    drivers: list[dict] = []
    chain: list[tuple[str, str, str, float | None, float | None]] = []  # (id, label, unit, cur, prev)

    def g(r: dict, k: str) -> float | None:
        return _n(r.get(k))

    reps1, reps0 = g(e1, "reps"), g(e0, "reps")
    rd1, rd0 = g(e1, "rep_days"), g(e0, "rep_days")
    tc1, tc0 = g(e1, "tc"), g(e0, "tc")
    pc1, pc0 = g(e1, "pc"), g(e0, "pc")
    dpr1, dpr0 = _div(rd1, reps1), _div(rd0, reps0)
    cpd1, cpd0 = _div(tc1, rd1), _div(tc0, rd0)
    pcp1, pcp0 = _div(pc1, tc1, 100), _div(pc0, tc0, 100)
    vpc1, vpc0 = _div(g(e1, "val"), pc1), _div(g(e0, "val"), pc0)
    have_edp = bool(e1 and e0 and rd0)

    if have_edp and family in ("field", "secondary", "coverage"):
        chain = [("reps", "Reps in the market", "count", reps1, reps0),
                 ("days_per_rep", "Days in market per rep", "number", dpr1, dpr0),
                 ("calls_per_day", "Calls per rep per day", "number", cpd1, cpd0)]
        if metric_id != "tc":
            chain.append(("pc_pct", "PC % (calls that ordered)", "percent", pcp1, pcp0))
        if family == "secondary":
            chain.append(("value_per_pc", "Order value per productive call", "currency", vpc1, vpc0))
        product = {"tc": "Total calls", "secondary": "Order value booked"}.get(
            metric_id if metric_id == "tc" else family, "Productive calls")
        _decompose(chain, drivers, product, exact=sign if metric_id in CHAIN_METRICS and sign != 0 else False)

    # ---- findings from the chain and the side metrics
    hol1, hol0 = g(e1, "holiday_days") or 0, g(e0, "holiday_days") or 0
    leave1, leave0 = g(e1, "leave_days") or 0, g(e0, "leave_days") or 0
    data_holiday = have_edp and hol1 - hol0 >= max(10, 0.03 * (rd0 or 0))
    data_holiday_prev = have_edp and hol0 - hol1 >= max(10, 0.03 * (rd0 or 0))
    holiday_days_cur = _holiday_dates(got.get("daily", []), w.cur)
    volume = unit in ("currency", "count")
    if volume and (wd1 < wd0 or hol_cur or data_holiday) and (sign < 0 or (sign == 0 and wd1 < wd0)):
        bits = []
        names = [h.name for h in hol_cur if h.field_off] or [h.name for h in hol_cur]
        if data_holiday:
            days_txt = ", ".join(d.strftime("%d %b").lstrip("0") for d in holiday_days_cur[:3])
            bits.append(f"{int(hol1)} rep-days were marked Holiday in the field app{' (mostly ' + days_txt + ')' if days_txt else ''}, "
                        f"vs {int(hol0)} in {w.prev_name.split(' (')[0]}")
        if wd1 != wd0:
            bits.append(f"there were {wd1} working days vs {wd0}")
        lead = (f"{' and '.join(names)} fell in this period" if names else "This period had fewer working days")
        txt = lead + (": " + "; ".join(bits) if bits else "") + "."
        if v1 is not None and v0 is not None and wd1 and wd0 and unit in ("currency", "count"):
            pw1, pw0 = v1 / wd1, v0 / wd0
            txt += f" Per working day, {metric.label} is {_chg(pw1, pw0, unit)} ({_v(pw1, unit)} vs {_v(pw0, unit)})."
        score = min(1.0, abs(_impact(drivers, "days_per_rep") or 0)) or (0.6 if data_holiday else 0.3)
        findings.append(Finding("holiday", txt, score + (0.5 if data_holiday else 0), likely=not data_holiday))
    elif volume and data_holiday_prev and sign > 0:
        findings.append(Finding("holiday", f"{w.prev_name.split(' (')[0].capitalize()} had {int(hol0)} rep-days marked Holiday "
                                           f"vs {int(hol1)} now, so part of the rise is simply more working days.", 0.6))

    for d in drivers:
        imp = d.get("impact")
        if imp is None or d["cur"] is None or d["prev"] is None:
            continue
        explains = (imp > 0.08)  # moves the same way as the metric
        if not explains:
            continue
        fid = {"reps": "fewer_reps", "days_per_rep": "leave_up", "calls_per_day": "fewer_calls", "pc_pct": "low_pc_pct",
               "value_per_pc": "low_drop_size"}.get(d["id"])
        if d["id"] == "days_per_rep" and any(f.id == "holiday" for f in findings):
            continue  # the holiday finding already says it
        if sign > 0:
            fid = "sustain"
        text = (f"{d['label']} {_chg(d['cur'], d['prev'], d['unit'])} ({_v(d['cur'], d['unit'])} vs {_v(d['prev'], d['unit'])}), "
                + (f"about {imp * 100:.0f}% of the change." if imp <= 1
                   else "more than the whole change (other drivers offset part of it)."))
        if d["id"] == "days_per_rep" and leave1 > leave0:
            text += f" Leave days rose to {int(leave1)} from {int(leave0)}."
        findings.append(Finding(fid or d["id"], text, min(imp, 1.0)))

    if metric_id == "pc_pct" and sign < 0:
        findings.append(Finding("low_pc_pct", f"Fewer visits turned into orders: {metric.label} {_chg(v1, v0, unit)} "
                                              f"({_v(v1, unit)} vs {_v(v0, unit)}).", 0.3))

    if have_edp and family in ("field", "secondary", "coverage"):
        fc1, fc0 = g(e1, "first_call"), g(e0, "first_call")
        late1, late0 = _div(g(e1, "late_days"), rd1, 100), _div(g(e0, "late_days"), rd0, 100)
        if fc1 is not None and fc0 is not None:
            drivers.append({"id": "first_call", "label": "Average first call", "unit": "time", "cur": fc1, "prev": fc0, "impact": None})
        if late1 is not None and late0 is not None:
            drivers.append({"id": "late", "label": "Days with first call after 10:30", "unit": "percent",
                            "cur": late1, "prev": late0, "impact": None})
        later = fc1 is not None and fc0 is not None and fc1 - fc0 >= 600
        if (later or (late1 is not None and late0 is not None and late1 - late0 >= 5)) and worse:
            findings.append(Finding("late_start", f"Reps started later: average first call {_v(fc1, 'time')} vs {_v(fc0, 'time')}, "
                                                  f"and {late1:.0f}% of market days started after 10:30 (was {late0:.0f}%).", 0.25))
        elif fc1 is not None and fc1 > LATE_SECONDS - 900 and worse:
            findings.append(Finding("late_start", f"Market start is late: the average first call is {_v(fc1, 'time')}, and "
                                                  f"{late1:.0f}% of market days started after 10:30.", 0.12))
        z1, z0 = _div(g(e1, "zero_pc_days"), rd1, 100), _div(g(e0, "zero_pc_days"), rd0, 100)
        if z1 is not None and z0 is not None:
            drivers.append({"id": "zero_pc", "label": "Market days with zero orders", "unit": "percent", "cur": z1, "prev": z0, "impact": None})
            if z1 - z0 >= 3 and worse:
                findings.append(Finding("zero_pc", f"{int(g(e1, 'zero_pc_days'))} rep-days had calls but no orders ({z1:.0f}% of "
                                                   f"market days, up from {z0:.0f}%).", 0.2))
            elif z1 >= 10 and worse:
                findings.append(Finding("zero_pc", f"{z1:.0f}% of market days had calls but no orders "
                                                   f"({int(g(e1, 'zero_pc_days'))} rep-days).", 0.1))
        lpc1, lpc0 = _div(g(e1, "lines"), tc1), _div(g(e0, "lines"), tc0)
        if lpc1 is not None and lpc0 is not None:
            drivers.append({"id": "lpc", "label": "Lines per call", "unit": "number", "cur": lpc1, "prev": lpc0, "impact": None})
            if _pct(lpc1, lpc0) is not None and _pct(lpc1, lpc0) <= -5 and worse:
                findings.append(Finding("low_lpc", f"Lines per call {_chg(lpc1, lpc0, 'number')} ({lpc1:.2f} vs {lpc0:.2f}).", 0.15))
        sc1, sc0 = g(e1, "sc"), g(e0, "sc")
        bp1, bp0 = _div(tc1, sc1, 100), _div(tc0, sc0, 100)
        if bp1 is not None and bp0 is not None:
            drivers.append({"id": "beat_adherence", "label": "Calls made vs planned (beat adherence)", "unit": "percent",
                            "cur": bp1, "prev": bp0, "impact": None})
            if bp1 - bp0 <= -5 and worse:
                findings.append(Finding("low_coverage", f"Reps covered less of their planned beat: calls were {bp1:.0f}% of "
                                                        f"planned calls vs {bp0:.0f}%.", 0.18))
        tp1, tp0 = _div(g(e1, "adhered_days"), rd1, 100), _div(g(e0, "adhered_days"), rd0, 100)
        if tp1 is not None and tp0 is not None and tp0 > 0:
            drivers.append({"id": "tour_plan", "label": "Days on the planned tour", "unit": "percent", "cur": tp1, "prev": tp0, "impact": None})
            if tp1 - tp0 <= -5 and worse:
                findings.append(Finding("tour_plan", f"Fewer days followed the tour plan: {tp1:.0f}% vs {tp0:.0f}%.", 0.12))
        if leave1 or leave0:
            drivers.append({"id": "leave", "label": "Rep-days on leave", "unit": "count", "cur": leave1, "prev": leave0, "impact": None})
        if hol1 or hol0:
            drivers.append({"id": "holiday_days", "label": "Rep-days marked Holiday", "unit": "count", "cur": hol1, "prev": hol0, "impact": None})

    # Side metrics from the catalog (UBO, drop size)
    for m in extra_metrics:
        a = _n((got.get(f"x_{m}_cur") or [{}])[0].get("v"))
        b = _n((got.get(f"x_{m}_prev") or [{}])[0].get("v"))
        if a is None or b is None:
            continue
        drivers.append({"id": m, "label": METRICS[m].label, "unit": METRICS[m].unit, "cur": a, "prev": b, "impact": None})
        p = _pct(a, b)
        if p is not None and p <= -5 and worse:
            fid = {"ubo": "ubo_down", "drop_size": "low_drop_size"}[m]
            if not any(f.id == fid for f in findings):
                findings.append(Finding(fid, f"{METRICS[m].label} {_chg(a, b, METRICS[m].unit)} "
                                             f"({_v(a, METRICS[m].unit)} vs {_v(b, METRICS[m].unit)}).", min(0.5, abs(p) / 100)))

    # Primary: distributors billed x value per distributor
    if family == "primary":
        pr = {r.get("p"): r for r in got.get("primary", [])}
        p1, p0 = pr.get("cur", {}), pr.get("prev", {})
        if p1 or p0:
            chain = [("dists", "Distributors billed", "count", g(p1, "dists"), g(p0, "dists")),
                     ("value_per_dist", "Billing per distributor", "currency",
                      _div(g(p1, "val"), g(p1, "dists")), _div(g(p0, "val"), g(p0, "dists")))]
            _decompose(chain, drivers, "Primary net invoice value", exact=sign if sign != 0 else False)
            drivers.append({"id": "invoices", "label": "Primary invoices", "unit": "count",
                            "cur": g(p1, "invoices"), "prev": g(p0, "invoices"), "impact": None})
            for d in drivers:
                if d.get("impact") and d["impact"] > 0.08 and d["id"] in ("dists", "value_per_dist"):
                    fid = ("distributors_not_billing" if d["id"] == "dists" else "low_invoice_value") if sign < 0 else "sustain"
                    findings.append(Finding(fid, f"{d['label']} {_chg(d['cur'], d['prev'], d['unit'])} "
                                                 f"({_v(d['cur'], d['unit'])} vs {_v(d['prev'], d['unit'])}), about "
                                                 f"{min(d['impact'], 1) * 100:.0f}% of the change.", min(d["impact"], 1.0)))

    # Coverage: visited vs assigned from the headline components
    if family == "coverage" and variant.combine is not None:
        num, den = variant.combine.num, variant.combine.den
        for comp, label in ((num, "Outlets visited" if metric_id == "coverage_pct" else "Outlets that ordered"),
                            (den, "Outlets in the plan" if den in ("assigned", "mapped") else den.upper())):
            a, b = _n(h1.get(f"p_{comp}")), _n(h0.get(f"p_{comp}"))
            if a is not None and b is not None:
                drivers.insert(0, {"id": comp, "label": label, "unit": "count", "cur": a, "prev": b, "impact": None})
        a1, a0 = _n(h1.get(f"p_{num}")), _n(h0.get(f"p_{num}"))
        b1, b0 = _n(h1.get(f"p_{den}")), _n(h0.get(f"p_{den}"))
        if sign < 0 and None not in (a1, a0, b1, b0):
            what = "Outlets visited" if metric_id == "coverage_pct" else "Outlets that ordered"
            findings.append(Finding("low_coverage", f"{what} {_chg(a1, a0, 'count')} ({_v(a1, 'count')} vs {_v(a0, 'count')}) "
                                                    f"while outlets in the plan {_chg(b1, b0, 'count')} ({_v(b1, 'count')} vs "
                                                    f"{_v(b0, 'count')}), so a smaller share of the plan was covered.", 0.4))

    # ---- contributors
    contributors = []
    for dim, v3 in contrib_variants.items():
        rows1, rows0 = got.get(f"c_{dim}_cur"), got.get(f"c_{dim}_prev")
        if rows1 is None or rows0 is None:
            continue
        block = _contrib(dim, v3, rows1, rows0, unit, sign or -1, v1, v0)
        if block["rows"]:
            contributors.append(block)
    concentrated = next((c for c in contributors if c["dim"] in ("zone", "region") and c.get("top_share", 0) >= 0.6), None)
    if concentrated and sign != 0:
        names = ", ".join(r["label"] for r in concentrated["rows"][:3])
        findings.append(Finding("region_drop" if sign < 0 else "sustain",
                                f"{'Most of the drop' if sign < 0 else 'Most of the rise'} comes from a few "
                                f"{_plural(DIM_LABELS[concentrated['dim']].lower())}: {names} " + (
                                    f"({concentrated['top_share'] * 100:.0f}% of the change)." if concentrated["top_share"] <= 1
                                    else "(they moved more than the whole net change; other areas moved the other way)."),
                                0.35))
    for sid, note, in_cur, explains in season_notes:
        if not explains:
            continue
        findings.append(Finding("season", ("This period" if in_cur else "The comparison period") + " falls in a known pattern. "
                                + note, 0.1, likely=True))

    # ---- reps to coach, and rep behaviour checked against the team's own numbers
    coach = _coach(got.get("reps")) if field_side else None
    rep_q = bool(_REP_WORDS.search(question.lower())) or metric_id == "pc_pct"
    if have_edp and (rep_q or sign == 0 or (family == "coverage" and worse)) and family in ("field", "secondary", "coverage"):
        have = {f.id for f in findings}
        late1, fc1 = _div(g(e1, "late_days"), rd1, 100), g(e1, "first_call")
        if "late_start" not in have and late1 is not None and late1 >= 15:
            findings.append(Finding("late_start", f"Late market starts: on {late1:.0f}% of market days the first call was after "
                                                  f"10:30 ({int(g(e1, 'late_days'))} rep-days); the average first call is "
                                                  f"{_v(fc1, 'time')}.", 0.3))
        z1 = _div(g(e1, "zero_pc_days"), rd1, 100)
        if "zero_pc" not in have and z1 is not None and z1 >= 3:
            findings.append(Finding("zero_pc", f"{int(g(e1, 'zero_pc_days'))} rep-days ({z1:.0f}% of market days) had calls but "
                                               f"not a single order.", 0.22))
        team = pcp1
        weak = [r for r in _rep_rows(got.get("reps")) if team and r["pc_pct"] < team / 2]
        if weak and team:
            findings.append(Finding("low_pc_pct", f"{len(weak)} reps converted fewer than {team / 2:.0f}% of their calls into "
                                                  f"orders, under half the team's PC % of {team:.0f}%; the weakest are listed "
                                                  f"below.", 0.28))
        tp1 = _div(g(e1, "adhered_days"), rd1, 100)
        if "tour_plan" not in have and tp1 is not None and 0 < tp1 < 90:
            findings.append(Finding("tour_plan", f"Reps followed the planned tour on only {tp1:.0f}% of market days.", 0.15))
    elif coach and coach["rows"] and worse and not any(f.id == "zero_pc" for f in findings):
        worst = coach["rows"][0]
        findings.append(Finding("zero_pc", f"The weakest converter, {worst['label']}, turned only {worst['pc_pct']:.0f}% of "
                                           f"{int(worst['tc'])} calls into orders; see the reps to coach below.", 0.05))

    findings.sort(key=lambda f: -f.score)
    if not findings and sign != 0:
        findings.append(Finding("region_drop" if sign < 0 else "sustain",
                                "None of the field drivers moved much, so the change is spread across areas; "
                                "see the biggest movers below.", 0.01))

    # ---- actions from the playbook
    book = playbook()
    actions = []
    seen = set()
    order = [f.id for f in findings if f.id not in ("season",)] + (["season"] if any(x[3] for x in season_notes) else [])
    if sign > 0:
        order = ["sustain"] + order
    for fid in order:
        if fid in seen or fid not in book or len(actions) >= 4:
            continue
        if sign > 0 and fid not in ("sustain", "season", "holiday"):
            continue
        seen.add(fid)
        actions.append({"title": book[fid]["title"], "steps": book[fid]["actions"][:3]})
    if not actions and coach and coach["rows"]:
        actions.append({"title": book["low_pc_pct"]["title"], "steps": book["low_pc_pct"]["actions"][:3]})

    # ---- headline sentence (template; always correct)
    where = (" for " + ", ".join(f"{DIM_LABELS.get(f.dim, f.dim)} {f.label}" for f in filters)) if filters else ""
    if v1 is None and v0 is None:
        summary = f"I found no {metric.label.lower()}{where} in either period, so there is nothing to explain yet."
    else:
        ch = _chg(v1, v0, unit)
        summary = f"{metric.label}{where} in {w.cur_name} is **{_v(v1, unit)}**, " + (
            f"{ch} from {_v(v0, unit)} in {w.prev_name}." if direction != "flat"
            else f"about the same as {_v(v0, unit)} in {w.prev_name} ({ch}).")
        if findings and direction != "flat":
            top = findings[0]
            summary += " The main reason" + (" (likely)" if top.likely else "") + ": " + top.text
        elif direction == "flat":
            summary += " Nothing changed much, but here is what is holding it back and what to do."

    why = [{"id": f.id, "text": f.text, "likely": f.likely} for f in findings[:6]]
    facts = f"Company: {ctx.name}\n{summary.replace('**', '')}\n" + "\n".join(
        ("Likely: " if f.likely else "") + f.text for f in findings[:6])

    chart, table = None, None
    if contributors:
        c0 = contributors[0]
        series_unit = c0["unit"]
        chart = {"type": "bar", "series": [{"id": "prev", "label": w.prev_name.split(" (")[0].capitalize(), "unit": series_unit},
                                           {"id": "cur", "label": w.cur_name.split(" (")[0].capitalize(), "unit": series_unit}],
                 "data": c0["rows"], "highlight": None, "x_label": DIM_LABELS[c0["dim"]]}
        table = {"columns": c0["columns"], "rows": c0["rows"]}

    return {
        "kind": "rca",
        "summary": summary,
        "summary_by": "template",
        "headline": _chg(v1, v0, unit) if direction != "flat" else "No big change",
        "facts": facts,
        "chart": chart,
        "table": table,
        "rca": {
            "metric": metric_id, "metric_label": metric.label, "unit": unit, "direction": direction, "worse": worse,
            "current": {"label": w.cur_name, "value": v1, "start": w.cur.start.isoformat(), "end": w.cur.end.isoformat()},
            "previous": {"label": w.prev_name, "value": v0, "start": w.prev.start.isoformat(), "end": w.prev.end.isoformat()},
            "change": _chg(v1, v0, unit),
            "why": why,
            "drivers": [_fmt_driver(d) for d in drivers],
            "calendar": calendar + [("Likely: " if ex else "Season: ") + n for _, n, _, ex in season_notes],
            "contributors": contributors,
            "coach": coach,
            "actions": actions,
        },
        "formula_notes": [],
        "notes": list(dict.fromkeys(notes)),
        "suggestions": _follow_ups(metric_id, family, w),
        "how": {
            "period": f"{w.cur_name} vs {w.prev_name}",
            "filters": ", ".join(f"{DIM_LABELS.get(f.dim, f.dim)} {f.label}" for f in filters) or "None",
            "metrics": [{"label": metric.label, "formula": variant.formula,
                         "source": "Pulse, same formula for both periods"},
                        {"label": "Driver tree", "formula": _tree_text(family, metric_id),
                         "source": "Salesmen's daily performance summaries (EmployeeDayPerformance)"
                         if family != "primary" else "Primary invoices (PrimaryReprimary)"},
                        {"label": "Ranking", "formula": "Each driver's share of the change = ln(driver now / driver before) "
                                                        "÷ ln(metric now / metric before); shares add up to 100%",
                         "source": "Calendar: backend/data/india_calendar.json; actions: backend/data/rca_playbook.json"}],
            "interpreted_by": "rules",
            "variant": variant.id,
            "queries": len(keys),
            "sql": sqls,
        },
    }


def _decompose(chain, drivers: list[dict], product_label: str, exact: bool | int = True) -> None:
    """Log decomposition: each multiplying driver's share of the product's change.

    `exact` is False when the metric asked about is not the product itself (PC % next to the PC
    chain): the drivers are then shown for context without shares. When it is the metric's direction
    (+1 / -1), shares are only given if the chain moved the same way."""
    vals = [(c[3], c[4]) for c in chain]
    ok = bool(exact) and all(a and b and a > 0 and b > 0 for a, b in vals)
    total = sum(math.log(a / b) for a, b in vals) if ok else 0.0
    if abs(total) < 0.02 or (exact is not True and exact * total < 0):
        ok = False  # the chain barely moved, or moved the other way: shares would be meaningless
    for (cid, label, unit, a, b) in chain:
        imp = None
        if ok and abs(total) > 1e-6:
            imp = math.log(a / b) / total
        drivers.append({"id": cid, "label": label, "unit": unit, "cur": a, "prev": b, "impact": imp, "chain": product_label})


def _impact(drivers: list[dict], did: str) -> float | None:
    for d in drivers:
        if d["id"] == did:
            return d.get("impact")
    return None


def _holiday_dates(daily: list[dict], p: Period) -> list[date]:
    """Days in the period where at least a quarter of day-starts were marked Holiday."""
    out = []
    for r in daily or []:
        try:
            d = date(int(str(r["d"])[:4]), int(str(r["d"])[4:6]), int(str(r["d"])[6:8]))
        except (KeyError, ValueError):
            continue
        if not (p.start <= d <= p.end):
            continue
        hol = _n(r.get("holiday_days")) or 0
        total = hol + (_n(r.get("rep_days")) or 0) + (_n(r.get("leave_days")) or 0) + (_n(r.get("off_days")) or 0)
        if total and hol / total >= 0.25:
            out.append(d)
    return out


def _contrib(dim: str, variant, rows1: list[dict], rows0: list[dict], unit: str, sign: int,
             v1: float | None, v0: float | None) -> dict:
    """Members that moved the metric most, in the direction it moved."""
    def index(rows):
        return {r.get("k0"): r for r in rows if r.get("k0") not in (None, 0, "")}
    a, b = index(rows1), index(rows0)
    truncated = len(rows1) >= config.MAX_ROWS or len(rows0) >= config.MAX_ROWS
    ratio = variant.combine is not None
    den = variant.combine.den if ratio else None
    items = []
    for k in set(a) | set(b):
        r1, r0 = a.get(k), b.get(k)
        if truncated and (r1 is None or r0 is None):
            continue
        c, p = _n((r1 or {}).get("v")), _n((r0 or {}).get("v"))
        name = str((r1 or r0).get("k0_name") or k).strip() or "Not mapped"
        if ratio or unit in ("percent", "time", "number"):
            if c is None or p is None:
                continue
            if ratio and min(_n(r1.get(f"p_{den}")) or 0, _n(r0.get(f"p_{den}")) or 0) < (50 if unit == "percent" else 20):
                continue
            if unit == "percent" and max(c, p) > 150:
                continue  # e.g. outlets visited off-plan: a share above 150% says nothing about the change
            delta = c - p
        else:
            c, p = c or 0.0, p or 0.0
            delta = c - p
        items.append({"label": name, "id": k, "prev": p, "cur": c, "delta": delta})
    if not (ratio or unit in ("percent", "time", "number")):  # same name under two parents: one row
        merged: dict[str, dict] = {}
        for x in items:
            m = merged.setdefault(x["label"].lower(), {**x, "prev": 0.0, "cur": 0.0, "delta": 0.0})
            m["prev"] += x["prev"]
            m["cur"] += x["cur"]
            m["delta"] += x["delta"]
        items = list(merged.values())
    want_down = sign < 0
    items.sort(key=lambda x: x["delta"] if want_down else -x["delta"])
    top = [x for x in items if (x["delta"] < 0 if want_down else x["delta"] > 0)][:5]
    total_delta = (v1 - v0) if (v1 is not None and v0 is not None and unit in ("currency", "count")) else None
    share = None
    if total_delta and top:
        share = sum(x["delta"] for x in top[:3]) / total_delta
        for x in top:
            x["share"] = x["delta"] / total_delta * 100
    for x in top:
        x["change"] = _chg(x["cur"], x["prev"], unit)
    label = DIM_LABELS[dim]
    cols = [{"key": "label", "label": label}, {"key": "prev", "label": "Before", "unit": unit},
            {"key": "cur", "label": "Now", "unit": unit}, {"key": "change", "label": "Change"}]
    if share is not None:
        cols.append({"key": "share", "label": "Share of change", "unit": "percent"})
    return {"dim": dim, "label": f"{'Biggest drops' if want_down else 'Biggest gains'} by {label.lower()}",
            "unit": unit, "rows": top, "columns": cols, "top_share": share if share and share > 0 else 0}


def _rep_rows(rows: list[dict] | None) -> list[dict]:
    out = []
    for r in rows or []:
        tc, pc = _n(r.get("tc")) or 0, _n(r.get("pc")) or 0
        name = str(r.get("name") or "").strip()
        if tc < 20 or re.match(r"(?i)^vacant\b", name):
            continue
        out.append({"label": str(r.get("name") or f"Rep {r.get('u')}").strip(), "days": _n(r.get("days")), "tc": tc, "pc": pc,
                    "pc_pct": pc / tc * 100 if tc else 0, "zero_pc_days": _n(r.get("zero_pc_days")),
                    "first_call": _n(r.get("first_call")), "late_days": _n(r.get("late_days"))})
    return out


def _coach(rows: list[dict] | None) -> dict | None:
    out = _rep_rows(rows)
    out.sort(key=lambda x: (x["pc_pct"], -(x["late_days"] or 0)))
    rows5 = out[:5]
    if not rows5:
        return None
    return {"title": "Reps to coach first (lowest PC %, at least 20 calls)",
            "columns": [{"key": "label", "label": "Sales rep"}, {"key": "days", "label": "Market days", "unit": "count"},
                        {"key": "tc", "label": "TC", "unit": "count"}, {"key": "pc", "label": "PC", "unit": "count"},
                        {"key": "pc_pct", "label": "PC %", "unit": "percent"},
                        {"key": "zero_pc_days", "label": "Zero-order days", "unit": "count"},
                        {"key": "first_call", "label": "Avg first call", "unit": "time"},
                        {"key": "late_days", "label": "Late starts", "unit": "count"}],
            "rows": rows5}


def _fmt_driver(d: dict) -> dict:
    return {"label": d["label"], "unit": d["unit"], "cur": d["cur"], "prev": d["prev"],
            "change": _chg(d["cur"], d["prev"], d["unit"]),
            "impact": None if d.get("impact") is None else round(d["impact"] * 100, 1),
            "chain": d.get("chain")}


def _tree_text(family: str, metric_id: str) -> str:
    if family == "primary":
        return "Primary value = Distributors billed × Billing per distributor"
    if metric_id == "tc":
        return "TC = Reps in market × Days per rep × Calls per rep-day"
    if family == "secondary":
        return "Order value = Reps × Days per rep × Calls per rep-day × PC % × Value per productive call; plus UBO and drop size"
    return "PC = Reps in market × Days per rep × Calls per rep-day × PC %; plus start time, zero-order days, LPC and beat adherence"


def _follow_ups(metric_id: str, family: str, w: Window) -> list[str]:
    m = METRICS[metric_id].label
    out = [f"{m} by day for the last 30 days", f"{m} by zone this month vs last month"]
    if family in ("field", "secondary"):
        out.append("Bottom 5 reps by PC % this month")
    elif family == "primary":
        out.append("Top 10 distributors by primary sales last month")
    return out


def _plural(word: str) -> str:
    return word[:-1] + "ies" if word.endswith("y") else word + "s"

