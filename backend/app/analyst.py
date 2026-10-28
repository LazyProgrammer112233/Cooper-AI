"""Analyst questions: several metrics, a ranking, two periods, filters and conditions in one prompt.

    "Compare the top 10 distributors in the North zone over the last 90 days against the previous 90 days.
     Identify distributors where primary sales grew by more than 15%, but secondary sales declined or
     remained flat."

parse_analysis() turns such a prompt into an Analysis plan (rules only, deterministic). run_analysis()
fetches every metric for every period through the same guarded catalog SQL as the rest of Cooper, then
ranks, filters, derives changes / shares / ratios and checks the conditions in Python. Every constraint
that was understood is listed in "How I got this"; any that can't be honoured is said out loud.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from . import fmt, indcal
from .catalog import DIM_LABELS, METRICS, PARAM_METRICS, pick_variant, with_param
from .interpreter import RawFilter, _extract_filters, _find_metrics, _normalise
from .sqlbuild import Filter, build_metric
from .timeparse import Period, add_months, find_period_span, month_end, month_start, preset

# --------------------------------------------------------------------------- vocabulary

NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "fifteen": 15, "twenty": 20, "fifty": 50}
ENTITY_WORDS = [
    ("distributor", r"distributors?|dbs?|stockists?|super stockists?"),
    ("rep", r"reps?|sales ?reps?|salesm[ae]n|sales ?persons?|sales ?people|dsrs?|isrs?|field users?|employees?"),
    ("outlet", r"outlets?|retailers?|stores?|shops?|counters?"),
    ("territory", r"territor(?:y|ies)|hqs?"),
    ("region", r"regions?"),
    ("zone", r"zones?"),
    ("state", r"states?"),
    ("city", r"cit(?:y|ies)|towns?"),
    ("category", r"categor(?:y|ies)"),
    ("brand", r"brands?"),
    ("sku", r"skus?|products?|items?"),
]
ENT_RX = "|".join(f"(?:{p})" for _, p in ENTITY_WORDS)
DEFAULT_RANK = {"distributor": "primary_sales", "rep": "pc", "brand": "secondary_sales", "category": "secondary_sales",
                "city": "secondary_sales", "sku": "secondary_sales", "outlet": "secondary_sales", "zone": "secondary_sales",
                "region": "secondary_sales", "territory": "secondary_sales", "state": "secondary_sales"}
FLAT_PCT = 2.0
MONTHS = r"january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
# words that can never be part of a place / brand name typed after "in" or "for"
BAD_NAME = re.compile(
    r"\b(them|it|which|each|every|their|those|these|that|this|decline\w*|drop\w*|fell|fall|grew|grow\w*|improv\w*|so|far|to|"
    r"wise|adherence|lowest|highest|worst|best|improvement|size|market|per|by|billed|nothing|primary|secondary|sales|"
    r"calls?|pc|tc|ubo|coverage|order|orders|value|outlets?|reps?|days?|weeks?|months?|quarter|year|weekdays?|sundays?|"
    r"kis|kaun|what|where|why|how|show|showing|tell|list|and|or|vs|than|average|share|growth|change|total|number|"
    r"was|were|is|are|it|fewer|more|less|smaller|bigger|q\d?|the|last|next|previous|past|first|earlier|same|"
    + MONTHS + r")\b")
SALES_WORDS = re.compile(r"\b(primary|secondary|sales|pc|tc|ubo|coverage|calls|order|value|drop|size|outlets|reps|distributors|"
                         r"penetration|lpc|growth|billing)\b")


def entity_of(word: str) -> str | None:
    w = word.strip().lower()
    for dim, pat in ENTITY_WORDS:
        if re.fullmatch(pat, w):
            return dim
    return None


def _per_word(mid: str | None) -> str:
    return {"working_reps": "rep", "ubo": "billed outlet", "pc": "productive call", "tc": "call",
            "market_days": "market day", "active_outlets": "outlet", "visited_outlets": "visited outlet"}.get(mid or "", mid or "")


# --------------------------------------------------------------------------- plan


@dataclass
class Ref:
    """A metric as asked: a catalog metric, a parameterised one, or one derived from two metrics."""
    kind: str  # base | ratio | per | gap | share | dayshare | sharediff
    a: str
    b: str | None = None
    param: float | None = None
    label: str = ""

    @property
    def key(self) -> str:
        p = f"@{int(self.param)}" if self.param is not None else ""
        return {"base": f"{self.a}{p}", "ratio": f"{self.a}/{self.b}", "per": f"{self.a} per {self.b}",
                "gap": f"{self.a}-{self.b}", "share": f"share:{self.a}", "dayshare": f"days%:{self.a}{p}",
                "sharediff": f"sharediff:{self.a}-{self.b}"}[self.kind]

    @property
    def unit(self) -> str:
        if self.kind in ("ratio", "share", "dayshare", "sharediff"):
            return "percent"
        if self.kind == "per":
            return "currency" if METRICS[self.a].unit == "currency" else "number"
        return METRICS[self.a].unit

    @property
    def higher_is_better(self) -> bool:
        return METRICS[self.a].higher_is_better if self.kind in ("base", "dayshare") else True

    def name(self) -> str:
        if self.label:
            return self.label
        la = METRICS[self.a].label
        if self.kind == "base":
            if self.a == "late_start_days" and self.param is not None:
                return f"Days with first call after {fmt.clock(self.param)}"
            if self.a == "early_end_days" and self.param is not None:
                return f"Days with last call before {fmt.clock(self.param)}"
            if self.a == "days_min_calls" and self.param is not None:
                return f"Days with at least {int(self.param)} calls"
            if self.a == "low_call_days" and self.param is not None:
                return f"Days with fewer than {int(self.param)} calls"
            return la
        lb = METRICS[self.b].label if self.b else ""
        if self.kind == "dayshare":
            base = Ref("base", self.a, param=self.param).name()
            return f"{base} (% of market days)"
        return {"ratio": f"{la} as % of {lb}", "per": f"{la} per {_per_word(self.b)}", "gap": f"{la} minus {lb}",
                "share": f"Share of {la.lower()}", "sharediff": f"Share of {la.lower()} minus share of {lb.lower()}"}[self.kind]

    def short(self) -> str:
        return {"primary_sales": "primary", "secondary_sales": "secondary", "pc_pct": "PC%", "tc": "TC", "pc": "PC",
                "ubo": "UBO", "ubo_pct": "UBO%", "drop_size": "drop size", "coverage_pct": "coverage", "lpc": "LPC",
                "order_value": "order value"}.get(self.a, self.name().lower()) if self.kind == "base" else self.name().lower()

    def bases(self) -> list[tuple[str, float | None]]:
        if self.kind == "base":
            return [(self.a, self.param)]
        if self.kind == "dayshare":
            return [(self.a, self.param), ("market_days", None)]
        if self.kind == "share":
            return [(self.a, None)]
        return [(self.a, None), (self.b, None)]


@dataclass
class Cond:
    ref: str  # Ref.key, or "faster:<a>|<b>" for "A growing faster than B"
    measure: str  # cur | prev | chg_pct | chg_pts | chg_abs | vs_avg
    op: str  # > < >= <= le_flat ge_flat flat
    value: float | None = None
    pre: bool = False  # narrows the list before ranking ("only reps with at least 200 calls")
    text: str = ""


@dataclass
class Analysis:
    entity: str | None = None
    extra_keys: list[str] = field(default_factory=list)
    filters: list[RawFilter] = field(default_factory=list)
    split: list[RawFilter] = field(default_factory=list)  # "Figaro and Bertolli", "Karnataka and Tamil Nadu side by side"
    top_n: int | None = None
    top_dir: str = "desc"
    rank: Ref | None = None
    rank_measure: str = "cur"
    filter_first: bool = False  # "top 10 X where <condition>": rank among the matches
    refs: list[Ref] = field(default_factory=list)
    period: Period | None = None
    compare: Period | None = None
    compare_kind: str = ""  # previous | last_year | explicit | avg
    baseline_n: int = 0
    baseline_grain: str = ""
    grain: str | None = None
    conds: list[Cond] = field(default_factory=list)
    sort: tuple[str, str, str] | None = None  # (ref key | "gapchg", measure, asc | desc)
    consecutive: int = 0
    weekday_split: bool = False
    share: bool = False
    contribution: bool = False
    drivers: list[str] = field(default_factory=list)
    composite: bool = False
    extremes: bool = False
    groups: list[tuple[str, int]] = field(default_factory=list)
    diagnose: bool = False
    drill: str | None = None  # "in which zones did each of them decline"
    chain: bool = False  # "... and which 5 reps in that zone ..."
    why: bool = False
    contrib_n: int = 5
    contrib_dims: list[str] = field(default_factory=list)
    sections: list["Analysis"] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "entity": self.entity, "extra_keys": self.extra_keys,
            "filters": sorted([f.text, f.dim_hint] for f in self.filters), "split": [f.text for f in self.split],
            "top": [self.top_n, self.top_dir, self.rank.key if self.rank else None, self.rank_measure] if self.top_n else None,
            "metrics": [r.key for r in self.refs],
            "period": [self.period.start.isoformat(), self.period.end.isoformat()] if self.period else None,
            "compare": [self.compare.start.isoformat(), self.compare.end.isoformat()] if self.compare else None,
            "compare_kind": self.compare_kind, "grain": self.grain,
            "conds": [[c.ref, c.measure, c.op, c.value, c.pre] for c in self.conds],
            "flags": sorted(k for k, v in {"consecutive": self.consecutive, "weekday": self.weekday_split,
                                           "share": self.share, "contribution": self.contribution,
                                           "drivers": bool(self.drivers), "composite": self.composite,
                                           "extremes": self.extremes, "groups": bool(self.groups),
                                           "diagnose": self.diagnose, "why": self.why, "drill": bool(self.drill),
                                           "chain": self.chain, "filter_first": self.filter_first}.items() if v),
            "sections": len(self.sections),
        }


# --------------------------------------------------------------------------- detection

_RANK_RX = re.compile(r"\b(top|bottom|best|worst|highest|lowest)\s+(\d+|ten|five|three|twenty)\b|"
                      r"\b\d+\s+(?:" + ENT_RX + r")\s+with\s+the\s+(?:lowest|highest|most|least)\b")
_COND_RX = re.compile(r"\b(more|less|fewer|greater|higher|lower) than\s+(?:₹|rs\.?\s*)?\d|\b(above|below|over|under|at least|at most)\s+"
                      r"(?:₹|rs\.?\s*)?\d|\b\d+(?:\.\d+)?\s*(%|percent|percentage points|points|pts)|\bflat\b|\bin a row\b|"
                      r"\bconsecutive\b|\bbilled nothing\b|\ball three\b|\bon all three\b|\bbelow the \w+ average\b")
_COMPARE_RX = re.compile(r"\b(vs\.?|versus|compared? (?:with|to|against)|against)\b|\b(previous|prior|preceding|same period|"
                         r"same dates|same days|same month|before that|earlier period|the \d+ (?:days|weeks|months) before|"
                         r"change|growth|grew|improv\w*|declin\w*|fell|went down|went up|months ago)\b")
_EACH_RX = re.compile(r"\b(for each|each|every|across(?: all)?|by)\s+(" + ENT_RX + r")\b|\b(" + ENT_RX + r")[- ]wise\b")


def is_analysis(text: str) -> bool:
    """A ranking plus another constraint, or conditions / several metrics over a list, or a multi-line ask."""
    if len([ln for ln in text.splitlines() if ln.strip()]) >= 2:
        return True
    t = _normalise(text)
    metrics = len(dict.fromkeys(m for _, m in _find_metrics(t)[0]))
    rank = bool(_RANK_RX.search(t))
    cond = bool(_COND_RX.search(t))
    comp = bool(_COMPARE_RX.search(t))
    each = bool(_EACH_RX.search(t)) or bool(re.search(r"\b(list|which|identify|find|flag|highlight)\b.*\b(" + ENT_RX + r")\b", t))
    sentences = len(re.findall(r"[.?]\s+\S", text.strip())) >= 1
    trend = bool(re.search(r"\b(month[- ]wise|week by week|month by month|week[- ]wise|day[- ]wise|in a row|consecutive|"
                           r"month[- ]on[- ]month|weekdays?|sundays?)\b", t))
    why = bool(re.match(r"^\s*(why|explain|where)\b", t))
    counted = bool(re.search(r"\b(?:top|bottom|the)?\s*\d+\s+(?:" + ENT_RX + r")\b", t))
    listing = bool(re.search(r"\b(whose|where|who|which|that)\b", t)) and bool(re.search(r"\b(" + ENT_RX + r")\b", t))
    two_periods = len(re.findall(r"\b(?:this|last|previous) (?:week|month|quarter|year)\b|\bq[1-4]\b|\bmtd\b|\blmtd\b", t)) >= 2
    explain = bool(re.search(r"\b(explain|why|actions?|doing wrong)\b", t))
    if why and (comp or two_periods or metrics >= 2 or each):
        return True  # scoped root cause: the analyst reads the scope, the RCA engine answers
    return (rank and (cond or comp or metrics >= 2 or sentences or trend or each or two_periods or explain)) or \
        (cond and (each or comp or listing)) or \
        (each and (comp or trend or metrics >= 2)) or (why and counted) or (rank and why) or \
        (trend and (why or listing or metrics >= 2 or bool(re.search(r"side by side|\band\b", t)))) or \
        bool(re.search(r"\bside by side\b|\baverage (?:monthly |weekly )?\w* ?(?:\w+ )?of the last\b", t))


# --------------------------------------------------------------------------- parsing helpers

_NUM = r"(\d+(?:\.\d+)?)"
_CMPW = (r"more than|greater than|over|above|at least|no less than|higher than|less than|fewer than|below|under|lower than|"
         r"at most|no more than|>=|<=|>|<")
OPS = {"more than": ">", "greater than": ">", "over": ">", "above": ">", "at least": ">=", "no less than": ">=",
       "higher than": ">", "less than": "<", "fewer than": "<", "below": "<", "under": "<", "lower than": "<",
       "at most": "<=", "no more than": "<=", ">=": ">=", "<=": "<=", ">": ">", "<": "<"}
GROW = (r"(?:grew|grown|grow|grows|growing|increased|increase|increases|rose|risen|went up|gone up|improved|improve|improves|"
        r"gained|higher|moved later)")
FALL = (r"(?:declined|decline|declines|dropped|drop|drops|fell|fallen|fall|falls|decreased|decrease|went down|gone down|lost|"
        r"lower|reduced|slipped|moved earlier)")
FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">="}


def _num(tok: str) -> float:
    return float(NUMS.get(tok, tok))


def _amount(value: float, unit: str | None) -> float:
    u = (unit or "").lower().strip()
    if u in ("lakh", "lakhs", "lac", "lacs", "l"):
        return value * 1e5
    if u in ("crore", "crores", "cr"):
        return value * 1e7
    if u in ("k", "thousand"):
        return value * 1e3
    return value


def _clock(h: float, m: float, ampm: str) -> float:
    return (h % 12 + (12 if ampm.lower() == "pm" else 0)) * 3600 + m * 60


def _blank(t: str, a: int, b: int) -> str:
    return t[:a] + " " * (b - a) + t[b:]


def periods_in(t: str, today: date, fy: int) -> list[tuple[Period, tuple[int, int]]]:
    """Every period phrase in the text, in order ("Q2 compared with Q1" -> [Q2, Q1])."""
    out = []
    work = t
    for m in re.finditer(r"\bfirst (\d+) days of (\w+)", work):
        hit = find_period_span(m.group(2), today, fy)
        if hit:
            s = hit[0].start
            e = min(s + timedelta(days=int(m.group(1)) - 1), hit[0].end)
            out.append((Period(s, e, f"First {m.group(1)} days of {s:%b %Y}", "custom"), m.span()))
            work = _blank(work, *m.span())
    for _ in range(4):
        hit = find_period_span(work, today, fy)
        if not hit:
            break
        p, (a, b) = hit
        out.append((p, (a, b)))
        work = _blank(work, a, b)
    out.sort(key=lambda x: x[1][0])
    return out


def _shift_year(p: Period, years: int = -1) -> Period:
    def sh(d: date) -> date:
        try:
            return d.replace(year=d.year + years)
        except ValueError:
            return d.replace(year=d.year + years, day=28)
    s, e = sh(p.start), sh(p.end)
    return Period(s, e, f"{s:%d %b %Y} to {e:%d %b %Y}", "last_year")


def _label(p: Period) -> str:
    s, e = p.start.strftime("%d %b").lstrip("0"), p.end.strftime("%d %b %Y").lstrip("0")
    return s if p.start == p.end else f"{s} to {e}"


def _previous(p: Period, today: date) -> tuple[Period, Period, list[str]]:
    from .rca import windows
    w = windows(p, today)
    return Period(w.cur.start, w.cur.end, p.label, p.preset), w.prev, w.notes


# --------------------------------------------------------------------------- the parser


def parse_analysis(question: str, today: date, fy: int = 4) -> Analysis:
    raw = question.strip()
    lines = [ln.strip(" -•\t") for ln in re.split(r"\n+|(?<=[.:?])\s+(?=\d+[.)]\s)", raw) if ln.strip(" -•\t")]
    numbered = [ln for ln in lines if re.match(r"^\d+[.)]\s+", ln)]
    if len(numbered) >= 2:
        context = " ".join(ln for ln in lines if ln not in numbered)
        subs = [_parse_one(f"{re.sub(r'^\d+[.)]\s+', '', ln)} {context}", today, fy) for ln in numbered]
        if len({(s.entity, s.period, s.compare) for s in subs}) == 1 and all(s.entity for s in subs):
            plan = subs[0]  # same list and periods: one table
            for s in subs[1:]:
                for r in s.refs:
                    if r.key not in [x.key for x in plan.refs]:
                        plan.refs.append(r)
            plan.conds = []
            if re.search(r"\b(all three|all four|all of them|on all)\b", context.lower()):
                down = bool(re.search(FALL, context.lower()))
                plan.conds = [Cond(r.key, _chg(r), "<" if down else ">", 0.0,
                                   text=f"{r.name()} {'went down' if down else 'went up'}") for r in plan.refs]
            plan.text = raw
            plan.notes.append(f"Your {len(numbered)} numbered asks cover the same {DIM_LABELS[plan.entity].lower()}s and "
                              "periods, so they're answered in one table.")
            return plan
        return Analysis(sections=subs, titles=[re.sub(r"^\d+[.)]\s+", "", ln) for ln in numbered], text=raw)
    one = " ".join(lines)
    # "Which zone has the biggest gap ..., and which 5 reps in that zone have the lowest PC%?"
    m = re.search(r",?\s+and\s+(which|show|list)\s+(.+?\bin (?:that|this|the same) (?:zone|region|state|territory|city)\b.*)$", one,
                  flags=re.I)
    if m:
        first = _parse_one(one[:m.start()], today, fy)
        second = _parse_one(m.group(1) + " " + m.group(2), today, fy)
        second.period, second.compare, second.compare_kind = first.period, first.compare, first.compare_kind
        second.chain = True
        first.top_n = first.top_n or 1
        return Analysis(sections=[first, second], titles=[one[:m.start()].strip(" ,"), (m.group(1) + " " + m.group(2)).strip()],
                        text=raw)
    return _parse_one(one, today, fy)


def _parse_one(question: str, today: date, fy: int) -> Analysis:
    a = Analysis(text=question)
    t = _normalise(question)
    t = re.sub(r"₹\s*", "rs ", t)
    t = re.sub(r"\brs\.?\s*(?=\d)", "rs ", t)
    t = re.sub(r"(\d),(\d)", r"\1\2", t)
    t = re.sub(r"\s+", " ", t).strip()
    tq = t.replace("drop size", "dropsize").replace("sales growth", "salesgrowth")  # so "drop size" isn't a fall

    # ---- why / where with a scope: the root cause engine, with N contributors
    a.why = bool(re.match(r"^\s*(why|explain|where|what is driving|what's driving|what caused)\b", t)) or \
        bool(re.search(r"\b(explain why|caused most|responsible|how much of (?:it|the \w+ drop) (?:is |was )?(?:explained|due))\b", t))
    m = re.search(r"\b(?:top|bottom|the)?\s*(\d+|five|ten|three)\s+(" + ENT_RX + r")\b", t) if a.why else None
    if m:
        a.contrib_n = int(_num(m.group(1)))

    # ---- periods
    found = periods_in(t, today, fy)
    ps = [p for p, _ in found]
    cur = ps[0] if ps else None
    avg = re.search(r"\baverage (?:monthly |weekly )?(?:[\w%]+ ){0,4}(?:of|over) the (?:last|past|previous) (\d+|\w+) (months?|weeks?)\b|"
                    r"\btheir average (week|month) over the (?:last|past) (\d+|\w+) (weeks?|months?)\b", t)
    before = re.search(r"\bin the (\d+) (days|weeks|months) before (?:that|it)\b|\bthe (\d+) (days|weeks|months) before\b(?! that)", t)
    fromto = re.search(r"\bfrom (" + MONTHS + r") to (" + MONTHS + r")\b", t)
    if avg:
        n = int(_num(avg.group(1) or avg.group(4)))
        grain = "month" if (avg.group(2) or avg.group(5) or "").startswith("month") else "week"
        a.compare_kind, a.baseline_n, a.baseline_grain = "avg", n, grain
        span = avg.span()
        keep = [p for p, s in found if not (s[0] >= span[0] and s[1] <= span[1] + 2)]
        cur = keep[0] if keep else preset("this_week" if grain == "week" else "mtd", today, fy)
    elif re.search(r"\bsame (period|dates|days|month|week|quarter)(?: as| of)? last year\b|\blast year same\b|\byear on year\b|\byoy\b", t) \
            and cur:
        a.compare, a.compare_kind = _shift_year(cur), "last_year"
    elif fromto and re.search(r"growth|grow|contribut|change", t) and not re.search(r"\b(vs|versus|compared|against)\b", t):
        c = find_period_span(fromto.group(2), today, fy)
        p_ = find_period_span(fromto.group(1), today, fy)
        if c and p_:
            cur, a.compare, a.compare_kind = c[0], p_[0], "explicit"
    elif before and cur:
        n = int(before.group(1) or before.group(3))
        unit = before.group(2) or before.group(4)
        days = n * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
        e = cur.start - timedelta(days=1)
        a.compare, a.compare_kind = Period(e - timedelta(days=days - 1), e, f"The {n} {unit} before", "custom"), "explicit"
    elif len(ps) >= 2 and ps[1] != ps[0] and re.search(r"\b(vs\.?|versus|compared?|against|with|and|than)\b", t):
        grain_words = re.search(r"\b(month[- ]wise|month by month|monthly|week by week|week[- ]wise|weekly|trend)\b", t)
        if grain_words and not re.search(r"\b(vs\.?|versus|compared?|against)\b", t):
            cur = ps[-1] if fromto else ps[0]  # "month-wise from April to September": the range is the trend window
        else:
            cur, a.compare, a.compare_kind = ps[0], ps[1], "explicit"
            if re.search(r"\bsame days\b", t) and a.compare.days > cur.days:
                a.compare = Period(a.compare.start, a.compare.start + timedelta(days=cur.days - 1), a.compare.label, "custom")
    ago = re.search(r"\b(\d+|two|three|six) months ago\b", t)
    if ago and not a.compare:
        s_ = add_months(month_start(today), -int(_num(ago.group(1))))
        a.compare, a.compare_kind = Period(s_, month_end(s_), f"{s_:%b %Y}", "month"), "explicit"
    if re.search(r"\bstill active\b", t) and a.compare_kind == "explicit":
        a.compare, a.compare_kind = None, ""
    if fromto and not a.compare:
        hit = find_period_span(f"from {fromto.group(1)} to {fromto.group(2)}", today, fy)
        if hit:
            cur = hit[0]
    if cur is None:
        cur = preset("mtd", today, fy)
        a.notes.append("No period given, so I used this month so far (MTD).")
    gw = re.search(r"\b(week by week|week[- ]wise|weekly|weeks? in a row)\b", t) and "week" or \
        re.search(r"\b(day[- ]wise|daily|day by day)\b", t) and "day" or \
        re.search(r"\b(month[- ]wise|month by month|monthly|month[- ]on[- ]month|mom|months? in a row|consecutive months)\b", t) and "month"
    if gw and a.compare_kind != "avg":
        a.grain = gw
    elif re.search(r"\btrend\b", t):
        a.grain = "week" if cur.days <= 70 else "month"
    a.weekday_split = bool(re.search(r"\bweekdays?\b.*\b(sunday|weekend)s?\b|\b(sunday|weekend)s?\b.*\bweekdays?\b", t))
    needs_compare = bool(re.search(r"\b(vs\.?|versus|compared?|against|change|changed|growth|grew|grow|growing|improv\w*|declin\w*|"
                                   r"drop|dropped|fell|fall|increase\w*|decrease\w*|went (?:up|down)|previous|prior|before that|"
                                   r"earlier|gained|lost|moved later|moved earlier|slow\w*)\b", t.replace("drop size", "dropsize")))
    if not a.compare and a.compare_kind != "avg" and needs_compare and not a.grain and not a.weekday_split:
        cur, prev, notes = _previous(cur, today)
        a.compare, a.compare_kind = prev, "previous"
        a.notes += notes
    elif a.compare and a.compare_kind == "explicit" and cur.end >= today - timedelta(days=1) and cur.preset in (
            "mtd", "this_week", "qtd", "ytd") and a.compare.days > cur.days and re.search(
            r"\b(sales|calls|pc|tc|ubo|orders?|outlets?|value|days)\b", t):
        # a month so far against a whole month would always look like a fall: compare the same days
        a.compare = Period(a.compare.start, a.compare.start + timedelta(days=cur.days - 1), a.compare.label, "custom")
        a.notes.append(f"{_label(cur)} is only part of the period, so I compared it with the same number of days "
                       f"({_label(a.compare)}).")
    a.period = cur

    # ---- filters (several values for one dimension: "Tamil Nadu and Kerala")
    ftext = re.sub(r"\b(top|bottom|best|worst|highest|lowest)\s+(\d+|ten|five|three)\b", " ", t)
    ftext = re.sub(r"\bfor (each|every|all)\b", " ", ftext)
    raw_filters, _, _ = _extract_filters(ftext)
    multi = re.compile(r"\b(?:in|for|of)\s+(?:the\s+)?((?:(?!\b(?:in|for|of|the)\b)[a-z]){2,25}?(?: [a-z]{2,15})?)\s+and\s+"
                       r"([a-z]{2,15}(?: [a-z]{2,15})?)(?:\s+(" + ENT_RX + r"))?(?=\s+(?:only|show|for|in|by|over|during|last|this|"
                       r"side|with|compared?|vs|categories|brands)\b|[,.?]|$)")
    for m in multi.finditer(t):
        x, y = m.group(1).strip(), m.group(2).strip()
        if SALES_WORDS.search(x + " " + y) or BAD_NAME.search(x + " " + y):
            continue
        dim = entity_of(m.group(3)) if m.group(3) else None
        raw_filters = [f for f in raw_filters if f.text not in (x, y, f"{x} and {y}") and not f.text.endswith(" " + y)]
        raw_filters += [RawFilter(x, dim), RawFilter(y, dim)]
    for m in re.finditer(r"\b(?:in|for|of|from)\s+(?:the\s+)?([a-z]+(?: [a-z]+)?)\s+(zone|region|state|territory|city|category|brand)s?\b",
                         t):
        if not BAD_NAME.search(m.group(1)):
            raw_filters.append(RawFilter(m.group(1), m.group(2)))
    for m in re.finditer(r"\b(?:in|from)\s+([a-z][a-z ]{2,25}?)\s+(?=who|whose|with|where|that|which|only|,)", t):
        raw_filters.append(RawFilter(m.group(1).strip(), None))
    for m in re.finditer(r"\b([a-z]+(?: [a-z]+)?) (brand|category) only\b|\b([a-z]+(?: [a-z]+)?) only\b", t):
        name = (m.group(1) or m.group(3) or "").strip()
        if name and not BAD_NAME.search(name) and not SALES_WORDS.search(name) and name not in ("days", "one"):
            raw_filters.append(RawFilter(name, m.group(2)))
    for m in re.finditer(r"\b(?:sales|sale|growth|penetration|ubo|coverage) of ([a-z]+)(?: ([a-z]+))?\b", t):
        name = m.group(1) if (not m.group(2) or BAD_NAME.search(m.group(2)) or SALES_WORDS.search(m.group(2))) \
            else f"{m.group(1)} {m.group(2)}"
        if not BAD_NAME.search(name) and not SALES_WORDS.search(name):
            raw_filters.append(RawFilter(name, None))
    for m in re.finditer(r"\b([a-z]+(?: [a-z]+)?) (category|brand)(?= (?:sales|sale|ubo|penetration|coverage|growth)\b)", t):
        name = re.sub(r"^(?:why|explain|did|the|for|in|of|show)\s+", "", m.group(1)).strip()
        if name and not BAD_NAME.search(name):
            raw_filters.append(RawFilter(name, m.group(2)))
    m = re.search(r"\bordered ([a-z][a-z ]{2,25}?) in the\b", t)
    if m and not BAD_NAME.search(m.group(1)):
        raw_filters.append(RawFilter(m.group(1).strip(), None))
    seen = set()
    for f in raw_filters:
        text = f.text.strip()
        cut = BAD_NAME.search(text)
        if cut:
            text = text[:cut.start()].strip()
        text = re.sub(r"\b(the|a|an|zone|region|state|city|brand|category)$", "", text).strip()
        text = re.sub(r"^(the|a|an)\s+", "", text).strip()
        if not text or len(text) < 2 or text in seen or re.fullmatch(r"(all|both|each|them|data|so)", text):
            continue
        seen.add(text)
        a.filters.append(RawFilter(text, f.dim_hint))
    # one filter can't be inside another ("north" and "north zone")
    a.filters = [f for f in a.filters if not any(g is not f and f.text in g.text.split() and g.dim_hint == f.dim_hint
                                                 for g in a.filters)]
    pair = re.search(r"\bcompare ([a-z]+) and ([a-z]+)\b", t)
    if pair and not SALES_WORDS.search(pair.group(0)) and not BAD_NAME.search(pair.group(1) + " " + pair.group(2)):
        a.split = [RawFilter(pair.group(1), None), RawFilter(pair.group(2), None)]
    elif re.search(r"\bside by side\b", t):
        same = [f for f in a.filters if f.dim_hint in (None, "state", "region", "zone")]
        if len(same) >= 2:
            a.split = same[:2]
    a.filters = [f for f in a.filters if f.text not in {s.text for s in a.split}]

    # ---- entity
    top = re.search(r"\b(?:the\s+)?(top|bottom|best|worst|highest|lowest|leading|weakest|least)\s+(\d+|ten|five|three|twenty|"
                    r"fifteen|two|four)?\s*(?:performing\s+|selling\s+)?(" + ENT_RX + r")\b", t)
    top2 = re.search(r"\b(?:the\s+)?(\d+|five|ten|three)\s+(" + ENT_RX + r")\s+with\s+the\s+(lowest|highest|least|most|biggest|"
                     r"smallest)\s+", t)
    groups = list(re.finditer(r"\b(top|bottom)\s+(\d+|ten|five)\s+(" + ENT_RX + r")\b", t))
    if len(groups) >= 2 and groups[0].group(1) != groups[1].group(1) and re.search(r"\bcompare|compared with\b", t):
        a.groups = [("desc" if g.group(1) == "top" else "asc", int(_num(g.group(2)))) for g in groups[:2]]
        a.entity = entity_of(groups[0].group(3))
    word, after = "", ""
    if top:
        a.entity = a.entity or entity_of(top.group(3))
        a.top_n = int(_num(top.group(2))) if top.group(2) else 10
        word, after = top.group(1), t[top.end():]
    elif top2:
        a.entity = entity_of(top2.group(2))
        a.top_n = int(_num(top2.group(1)))
        word, after = top2.group(3), t[top2.end() - len(top2.group(3)) - 6:]
    hw = re.search(r"\b(" + ENT_RX + r")\s+(?:\w+\s+){0,6}?(?:with|had|have|has) the (highest|lowest|most|least|biggest|largest)\b",
                   t) if not a.top_n and not re.search(r"highest and (?:the )?lowest", t) else None
    if hw and not (a.why and re.match(r"^\s*why", t)):
        a.entity = entity_of(hw.group(1))
        single = re.match(r"^\s*which (?:" + "|".join(p for _, p in ENTITY_WORDS) + r")\s+(?:has|had|is|was)\b", t) and \
            not re.search(r"\b(" + ENT_RX + r")s\b", hw.group(1) + " ")
        a.top_n, word = (1 if single else 10), hw.group(2)
        after = t[hw.end() - len(hw.group(2)) - 9:]
    if not a.entity:
        m = re.search(r"^\s*(?:which|list|show(?: me)?|identify|find|give me)\s+(" + ENT_RX + r")\b", t)
        if m and entity_of(m.group(1)):
            a.entity = entity_of(m.group(1))
    if not a.entity:
        m = re.search(r"\b(?:for each|each|every|across(?: all)?|for all)\s+(" + ENT_RX + r")\b|\b(" + ENT_RX + r")[- ]wise\b|"
                      r"\bby (zone|region|territory|state|city)\b", t)
        if m and not (a.grain and m.group(3)):
            a.entity = entity_of(next(g for g in m.groups() if g))
    if not a.entity and not (a.grain and not re.search(r"\b(each|every|for all)\b", t)):
        for m in re.finditer(r"\b(" + ENT_RX + r")\b", t):
            dim = entity_of(m.group(1))
            before_ = t[max(0, m.start() - 30):m.start()]
            if dim and not any(f.dim_hint == dim and f.text in before_ for f in a.filters) and \
                    not re.search(r"(per|of|number of|serving|serve|with at least|with more than|\d)\s*$", before_) and \
                    not re.match(r"\s*(who started|that ordered|ordered|coverage|penetration|additions|base)", t[m.end():]):
                a.entity = dim
                break
    if not a.entity and a.grain and re.search(r"\bby (zone|region|territory|state|city)\b", t):
        a.entity = re.search(r"\bby (zone|region|territory|state|city)\b", t).group(1)
    if a.entity and a.entity != "zone" and re.search(r"\b(by zone|zone[- ]wise)\b", t) and a.entity in ("brand", "category"):
        a.extra_keys = ["zone"]
    if a.entity == "rep" and re.search(r"\btheir region\b|\bregion and manager\b", t):
        a.extra_keys = ["region"]

    # ---- metrics: derived ones and parameterised ones first, then everything the glossary knows
    refs: list[Ref] = []

    def add(r: Ref) -> Ref:
        for x in refs:
            if x.key == r.key:
                return x
        refs.append(r)
        return r

    work = t
    derived = [
        (r"(order value|secondary sales|primary sales|sales) per productive call", lambda m: Ref("per", _mid(m.group(1)), "pc")),
        (r"(secondary sales|primary sales|sales|order value|productive calls|total calls) per (?:rep|salesman)\b(?! per day)",
         lambda m: Ref("per", _mid(m.group(1)), "working_reps")),
        (r"(secondary sales|primary sales|sales|order value) per (?:billed )?outlet", lambda m: Ref("per", _mid(m.group(1)), "ubo")),
        (r"productive calls? per rep per day|pc per rep per day|productive calls per day", lambda m: Ref("base", "pc_per_day")),
        (r"(?:average )?calls (?:per|a) day|calls per rep per day", lambda m: Ref("base", "calls_per_day")),
        (r"\bthe gap between (?:them|primary and secondary)|gap between primary and secondary", lambda m: Ref("gap", "primary_sales", "secondary_sales")),
        (r"secondary(?: sales)? (?:are |is )?(?:less|more) than \d+% of primary", lambda m: Ref("ratio", "secondary_sales", "primary_sales")),
        (r"secondary(?: sales)? is higher than primary", lambda m: Ref("ratio", "secondary_sales", "primary_sales")),
        (r"less than \d+% of (?:their )?outlets billed|% of outlets billed", lambda m: Ref("ratio", "ubo", "visited_outlets",
                                                                                         label="Billed outlets as % of visited outlets")),
        (r"share of primary sales and their share of secondary sales|share of primary and secondary",
         lambda m: Ref("sharediff", "primary_sales", "secondary_sales")),
        (r"\bgap between total calls and productive calls\b", lambda m: Ref("gap", "tc", "pc")),
        (r"zero[- ]order days(?: \(calls but no orders\))?|days with (?:calls but )?no orders?|zero[- ]pc days", lambda m: Ref("base", "zero_order_days")),
        (r"\boutlets ordered\b|\boutlets that ordered\b", lambda m: Ref("base", "productive_outlets")),
        (r"\b(?:number of )?(?:billed outlets|outlets (?:they serve|served|billed))\b", lambda m: Ref("base", "ubo")),
    ]
    for pat, make in derived:
        m = re.search(pat, work)
        if m:
            add(make(m))
            work = _blank(work, *m.span())
    for pat, mid, kind in [
        (r"(?:first call|started their first call|start(?:ed)? (?:their )?(?:first call|day))\w* after (\d{1,2})(?::(\d\d))?\s*(am|pm)", "late_start_days", "base"),
        (r"last call (?:of the day )?(?:was )?before (\d{1,2})(?::(\d\d))?\s*(am|pm)", "early_end_days", None),
        (r"(?:starting|started|start) (?:their |the )?day before (\d{1,2})(?::(\d\d))?\s*(am|pm)", "early_start_days", "dayshare"),
    ]:
        m = re.search(pat, t)
        if m:
            k = kind or ("dayshare" if re.search(r"% of days|percent of days", t) else "base")
            add(Ref(k, mid, param=_clock(float(m.group(1)), float(m.group(2) or 0), m.group(3))))
            work = _blank(work, *m.span())
    m = re.search(r"days with at least (\d+) calls", t)
    if m:
        add(Ref("base", "days_min_calls", param=float(m.group(1))))
        work = _blank(work, *m.span())
    m = re.search(r"fewer than (\d+) calls on more than (\d+)", t)
    if m:
        add(Ref("base", "low_call_days", param=float(m.group(1))))
        work = _blank(work, *m.span())
    for pat, mid in [(r"\bmarket days\b|\bworking days\b|\bdays worked\b|\bworked (?:at least |for )?\d+ days", "market_days"),
                     (r"\bholiday and leave days\b|\bholiday days\b|\bholiday and leave\b", "holiday_leave_days"),
                     (r"\bleave or absent\b|\babsent or (?:on )?leave\b|\bleave days\b|\babsent\b", "leave_absent_days"),
                     (r"\baverage bill value\b|\bavg\.? bill value\b|\baverage order value\b", "drop_size"),
                     (r"% of (?:their )?outlet base\b|\binactive outlets? %", "inactive_outlets_pct"),
                     (r"\blast call\b", "avg_last_call_time"),
                     (r"\battendance\b|\breps who started their day\b|\bstarted their day\b", "working_reps"),
                     (r"\btime (?:spent )?in (?:the )?market\b|\bretailing time\b", "time_in_market"),
                     (r"\btour plan\b|\bpjp adherence\b|\bbeat adherence\b", "tour_plan_adherence_pct"),
                     (r"\bnumber of reps\b|\breps went down\b|\brep count\b|\bheadcount\b|\bat least \d+ reps\b", "working_reps"),
                     (r"\bnumber of orders\b", "orders"),
                     (r"\bserving more than \d+ outlets\b|\bwith more than \d+ outlets\b", "ubo"),
                     (r"\bmore than \d+ outlets in total\b|\boutlet base\b|\btotal outlets\b", "active_outlets")]:
        m = re.search(pat, work)
        if m and not (mid == "avg_last_call_time" and any(r.a == "early_end_days" for r in refs)):
            add(Ref("base", mid))
            work = _blank(work, *m.span())
    for _, mid in _find_metrics(work)[0]:
        if mid in METRICS:
            add(Ref("base", mid))
    if re.search(r"\bsales\b|\bsalesgrowth\b|\bbusiness\b", tq) and not any(r.a in ("primary_sales", "secondary_sales")
                                                                            or (r.kind == "per" and "sales" in r.a) for r in refs):
        add(Ref("base", "secondary_sales"))
    if re.search(r"\bbilled\b|\bbilling\b", t) and a.entity == "distributor" and not any(r.a == "primary_sales" for r in refs) \
            and not re.search(r"billed outlets|outlets billed", t):
        add(Ref("base", "primary_sales"))
    if not refs:
        add(Ref("base", "secondary_sales"))
        a.notes.append("No metric named, so I used secondary sales.")
    if a.weekday_split:
        a.compare, a.compare_kind = None, ""

    # ---- ranking metric
    if a.top_n or a.groups:
        rank_ref = None
        rm = re.search(r"\b(?:by|on|in terms of|based on|with the (?:most|highest|lowest|least|biggest)|(?:had|have|has) the "
                       r"(?:most|highest|lowest|least|biggest))\s+(.{0,70})", after[:90])
        if rm and re.match(r"\s*(?:more|less|over|under|at least|\d)", rm.group(1)):
            rm = None
        if rm:
            sub = rm.group(1)
            best = None
            for r in refs:
                mm = re.search(_name_rx(r), sub)
                if mm and (best is None or mm.start() < best[0]):
                    best = (mm.start(), r)
            rank_ref = best[1] if best else None
            if rank_ref is None:
                hits = _find_metrics(sub)[0]
                if hits:
                    rank_ref = add(Ref("base", hits[0][1]))
            if re.match(r"\s*(?:(?:secondary|primary) )?(?:salesgrowth|sales growth|growth)\b", sub.replace("salesgrowth", "sales growth")):
                a.rank_measure = "chg_pct"
                rank_ref = rank_ref or add(Ref("base", "secondary_sales"))
                if not a.compare:
                    cur2, prev, _ = _previous(a.period, today)
                    a.period, a.compare, a.compare_kind = cur2, prev, "previous"
        if rank_ref is None:
            m = re.search(r"\b(" + "|".join(sorted({_name_rx(r) for r in refs}, key=len, reverse=True)) + r")\s+(?:ke hisab se|by)\b", t) \
                if refs else None
            if m:
                rank_ref = next((r for r in refs if re.fullmatch(_name_rx(r), m.group(1))), None)
        default = DEFAULT_RANK.get(a.entity or "", "secondary_sales")
        cond_first = bool(re.search(r"\b(top|bottom)\s+\d+\s+(?:" + ENT_RX + r")\s+(?:where|whose|that|which|with)\b|"
                                    r"^\s*which (?:top|bottom)\s+\d+\s+(?:" + ENT_RX + r")\s+(?:grew|improved|declined|fell|"
                                    r"dropped|had|have)\b|\bidentify (?:" + ENT_RX + r")\b.*\bshow the top \d+ by\b", t))
        if rank_ref is None and cond_first:
            a.filter_first = True
        if rank_ref is None:
            pool = [r for r in refs if r.kind == "base"]
            if default in [r.a for r in pool] or not pool or a.entity in ("distributor", "rep", "brand", "category", "city", "sku"):
                rank_ref = add(Ref("base", default))
                a.notes.append(f"Ranked by {rank_ref.name().lower()}, the usual measure for "
                               f"{DIM_LABELS.get(a.entity or '', 'item').lower()}s.")
            else:
                rank_ref = pool[0]
                a.notes.append(f"Ranked by {rank_ref.name().lower()}.")
        a.rank = rank_ref
        hib = rank_ref.higher_is_better
        if word in ("highest", "most", "biggest"):
            a.top_dir = "desc"
        elif word in ("lowest", "least", "smallest"):
            a.top_dir = "asc"
        elif word in ("top", "best", "leading"):
            a.top_dir = "desc" if hib else "asc"
        else:
            a.top_dir = "asc" if hib else "desc"
        if re.search(r"\bby the gap\b", t):
            a.filter_first = True
        mw = re.search(r"\b(?:with|had|have|has) the (most|highest|biggest|largest|least|lowest|smallest)\b", after[:60] or t)
        if mw:
            a.top_dir = "desc" if mw.group(1) in ("most", "highest", "biggest", "largest") else "asc"
        if rank_ref.kind == "sharediff":
            a.rank_measure = "abs"
        if a.contribution:
            a.rank_measure = "chg_abs"
    if not a.top_n:
        m = re.search(r"\btop (\d+) by the gap\b", t)
        if m:
            a.top_n, a.filter_first = int(m.group(1)), True
    a.refs = refs

    # ---- conditions
    a.conds = _conditions(t, refs, a)
    if a.filter_first and a.conds and a.rank and not re.search(r"\b(?:by|on) (?:the )?\w", after[:12]):
        c0 = next((c for c in a.conds if c.measure.startswith("chg")), a.conds[0])
        a.rank_measure = c0.measure if c0.measure != "cur" else "cur"
        a.rank = next((r for r in refs if r.key == c0.ref), a.rank)
        a.top_dir = "asc" if c0.op in ("<", "<=", "le_flat") else "desc"
        a.notes = [n for n in a.notes if not n.startswith("Ranked by")]
        a.notes.append(f"Among the {DIM_LABELS.get(a.entity, '').lower()}s that match, ranked by the change in {a.rank.name().lower()}.")
    if re.search(r"\bby the gap\b", t) and len([c for c in a.conds if c.measure.startswith("chg")]) >= 2:
        c1, c2 = [c for c in a.conds if c.measure.startswith("chg")][:2]
        a.sort = ("gapchg", f"{c1.ref}|{c2.ref}", "desc")
        a.rank = next(r for r in refs if r.key == c1.ref)
        a.rank_measure, a.top_dir, a.filter_first = "gapchg", "desc", True

    # ---- sorting, flags
    if a.sort is None:
        if re.search(r"sorted by the size of the drop|biggest drop|largest drop|ranked from worst", t):
            r0 = next((r for r in refs if any(c.ref == r.key for c in a.conds)), refs[0])
            a.sort = (r0.key, "vs_avg" if a.compare_kind == "avg" else _chg(r0), "asc")
        elif re.search(r"\b(?:lose|lost|losing) the most\b|\bbiggest (?:fall|decline|loss)\b", t):
            r0 = a.rank or refs[0]
            a.sort = (r0.key, _chg(r0), "asc")
        elif re.search(r"from best to worst improvement|ranked from best|best to worst", t):
            a.sort = (refs[0].key, _chg(refs[0]), "desc")
        elif re.search(r"ranked from (?:the )?lowest|from lowest|lowest first", t):
            r0 = next((r for r in refs if any(c.ref == r.key for c in a.conds)), refs[-1])
            a.sort = (r0.key, "cur", "asc")
    if a.grain and not a.compare:
        a.conds = [c for c in a.conds if not c.measure.startswith("chg")]
        if re.search(r"dropped the most|fell the most|biggest drop|lowest|highest|most|least", t):
            a.extremes = True
    if not a.entity and not a.split and len(a.filters) >= 2 and len({f.dim_hint for f in a.filters}) == 1 and \
            (a.weekday_split or a.grain):
        a.split, a.filters = a.filters[:2], a.filters[2:]
    m = re.search(r"\b(\d+|two|three)\s*(?:or more\s+)?(?:consecutive|(?:weeks|months|days)?\s*in a row)\b", t)
    if m:
        a.consecutive = int(_num(m.group(1)))
        a.grain = a.grain or ("week" if "week" in t else "month")
        a.conds = [c for c in a.conds if c.measure == "cur"]
    a.share = bool(re.search(r"\b(gained|lost) the most share\b|\bshare of (?:the )?total\b(?! growth)|\bshare\b", t)) and \
        not re.search(r"share of (?:the )?total growth|share of reps|share of days|share of primary sales and", t)
    a.contribution = bool(re.search(r"contribut\w* (?:the )?most to (?:the )?growth|share of (?:the )?total growth", t))
    if a.contribution and a.rank:
        a.rank_measure = "chg_abs"
    a.composite = bool(re.search(r"overall (?:productivity|improvement|performance)|rank \w+ by overall", t))
    a.extremes = bool(re.search(r"highest and (?:the )?lowest|best and (?:the )?worst|gained the most share and which|"
                                r"gained the most\b.*\blost the most|\bwhich .* most and which .* least", t))
    if re.search(r"whether the growth came from|which metric drove|drove the change|was it (?:fewer|more)|came from more calls", t):
        if re.search(r"more calls, a better pc%?, or a bigger drop size|came from more calls", t):
            for mid in ("tc", "pc_pct", "drop_size"):
                add(Ref("base", mid))
            a.drivers = ["tc", "pc_pct", "drop_size"]
        elif re.search(r"fewer outlets, smaller orders, or fewer calls", t):
            for mid in ("ubo", "drop_size", "tc"):
                add(Ref("base", mid))
            a.drivers = ["ubo", "drop_size", "tc"]
        else:
            a.drivers = [r.key for r in refs if r.kind == "base" and r is not a.rank][:4]
    a.diagnose = bool(re.search(r"\bexplain why\b|\bto explain why\b|\bgive \d+ actions\b|\bdoing wrong\b|\bactions? for each\b", t)) \
        and bool(a.entity) and (bool(a.top_n) or bool(a.groups))
    if a.diagnose:
        for mid in ("calls_per_day", "avg_call_start_time", "lpc", "drop_size", "pc_pct"):
            if mid not in [r.a for r in refs] and a.entity in ("rep", "zone", "region", "territory"):
                add(Ref("base", mid))
    if a.diagnose or a.groups:
        a.why = False
    if a.why and not re.match(r"^\s*(why|explain)\b", t) and a.top_n and not re.search(r"\b(why|caused|responsible|explain)\b", t) \
            and not re.match(r"^\s*where (?:did|is|are|was|were)\b.*\b(drop|fell|fall|declin|losing|lost|down)", t):
        a.why = False  # "show regions with the biggest increase": a ranking, not a root cause
    if a.why:
        asked = re.finditer(r"\b(?:which|the|top|bottom|show|by|and|of|\d+|five|ten|three)\s+(?:\d+\s+)?(" + ENT_RX + r")\b(?!\s+(?:sales|"
                            r"coverage|penetration|ubo))|\b(" + ENT_RX + r")\s+(?:are|is|were) responsible\b|\b(" + ENT_RX + r")-?wise\b", t)
        a.contrib_dims = list(dict.fromkeys(entity_of(next(g for g in m.groups() if g)) for m in asked
                                            if not any(f.dim_hint == entity_of(next(g for g in m.groups() if g)) for f in a.filters)))
        a.contrib_dims = [d for d in a.contrib_dims if d]
    m = re.search(r"\bin which (" + ENT_RX + r") did each\b|\bwhich (" + ENT_RX + r") did each\b|\btop (\d+) (" + ENT_RX + r") in each\b", t)
    if m:
        a.drill = entity_of(next(g for g in (m.group(1), m.group(2), m.group(4)) if g))
        if m.group(4):
            main = re.search(r"\b(?:show|list)\s+(" + ENT_RX + r")\b", t)
            if main and entity_of(main.group(1)) != a.drill:
                a.entity = entity_of(main.group(1))
                a.top_n = 10
                rm2 = re.search(r"\b(?:biggest|largest|most) (?:increase in )?([a-z ]+?)(?: this| last|$)", t)
                hits = _find_metrics(rm2.group(1))[0] if rm2 else []
                a.rank = next((r for r in refs if hits and r.a == hits[0][1]), refs[0])
                a.top_dir, a.rank_measure = "desc", "cur"
    if a.why and a.entity and not a.top_n:
        a.contrib_n = a.contrib_n or 5

    # ---- what can't be honoured
    for pat, what in [(r"\bmanager\b", "the reps' manager names"),
                      (r"\bstill active\b", "whether those new outlets are still active"),
                      (r"\bsold at least once in each\b", "\"sold at least once in each of the previous 3 months\""),
                      (r"\bordered at least once\b", "\"ordered at least once\" for new outlets"),
                      (r"\bsales lost from them\b", "the sales lost from outlets that turned inactive"),
                      (r"\bpc%? on those days\b", "PC% on only the late-start days"),
                      (r"\btotal calls on those days\b", "total calls on only the zero-order days"),
                      (r"\bthat were active last month\b", "which outlets were active last month (Pulse keeps only today's outlet status)")]:
        if re.search(pat, t):
            a.unsupported.append(what)
    if any(r.a in ("inactive_outlets", "active_outlets") for r in refs) and (a.compare or re.search(r"this (quarter|month)", t)):
        a.notes.append("Inactive and active outlets are today's snapshot in Pulse, so they can't be split by period.")
    return a


def why_metric(plan: Analysis) -> str:
    """The metric a "why" question is about: the earliest one it names that the root cause engine can explain."""
    from .rca import FAMILY
    t = _normalise(plan.text)
    best = None
    for r in plan.refs:
        if r.kind != "base" or not FAMILY.get(r.a):
            continue
        m = re.search(_name_rx(r), t)
        pos = m.start() if m else 10_000
        if best is None or pos < best[0]:
            best = (pos, r.a)
    return best[1] if best else (plan.refs[0].a if plan.refs else "secondary_sales")


def _mid(word: str) -> str:
    return {"order value": "order_value", "secondary sales": "secondary_sales", "primary sales": "primary_sales",
            "sales": "secondary_sales", "productive calls": "pc", "total calls": "tc"}.get(word.strip(), "secondary_sales")


_RX = {"pc": r"\bpc\b(?!%| ?percent)|productive calls?|productive call count", "tc": r"\btc\b|total calls?|\bcalls\b(?! (?:per|a) day)",
       "pc_pct": r"pc ?%|pc percentage|strike rate|productivity|converted", "primary_sales": r"primary(?: sales| sale| billing)?|\bbilled\b|\bbilling\b",
       "secondary_sales": r"secondary(?: sales| sale)?|(?<!primary )\bsales\b|\bsale\b|salesgrowth",
       "ubo": r"\bubo\b(?! ?%)|billed outlets|outlets they serve|outlets served|number of outlets", "ubo_pct": r"\bubo ?%",
       "drop_size": r"drop ?size|bill value|average bill", "lpc": r"lines per call|\blpc\b", "order_value": r"order value",
       "coverage_pct": r"coverage", "penetration_pct": r"penetration", "avg_call_start_time": r"first call|call start|start time",
       "market_days": r"market days|working days|worked", "calls_per_day": r"calls (?:per|a) day|calls per rep per day",
       "pc_per_day": r"productive calls per rep per day|pc per rep per day", "working_reps": r"number of reps|\breps\b|attendance",
       "leave_absent_days": r"leave|absent", "zero_order_days": r"zero[- ]order|no orders", "orders": r"(?<!zero-)\borders\b|number of orders",
       "time_in_market": r"time (?:spent )?in (?:the )?market", "tour_plan_adherence_pct": r"tour plan",
       "late_start_days": r"after \d|late", "early_end_days": r"last call|before \d", "days_min_calls": r"days with at least \d+ calls",
       "low_call_days": r"fewer than \d+ calls", "new_outlets": r"new outlets?|outlet additions", "inactive_outlets": r"inactive outlets?",
       "active_outlets": r"active outlets?|outlet base|outlets in total", "visited_outlets": r"outlets visited|visited outlets",
       "productive_outlets": r"outlets (?:that )?ordered", "avg_calls_per_rep": r"calls per rep", "holiday_leave_days": r"holiday",
       "avg_last_call_time": r"last call", "primary_invoice_value": r"primary invoice", "secondary_invoice_value": r"secondary invoice"}


def _name_rx(r: Ref) -> str:
    if r.kind == "base":
        return _RX.get(r.a, re.escape(METRICS[r.a].label.lower().split(" (")[0]))
    if r.kind == "per":
        return re.escape(r.a.split("_")[0]) + r".{0,20}per"
    if r.kind == "ratio" and r.a == "secondary_sales":
        return r"of primary|higher than primary"
    if r.kind == "dayshare":
        return _RX.get(r.a, r.a)
    if r.kind == "sharediff":
        return r"difference between (?:their )?share|share of primary"
    if r.kind == "gap":
        return r"\bgap\b"
    return re.escape(r.name().lower())


def _ref_for(span: str, refs: list[Ref]) -> Ref | None:
    """The metric a condition talks about: the metric named closest before the condition in its clause."""
    best, best_pos = None, -1
    for r in refs:
        for m in re.finditer(_name_rx(r), span):
            if m.start() >= best_pos:
                best, best_pos = r, m.start()
    return best


def _chg(r: Ref) -> str:
    return "chg_pts" if r.unit == "percent" else ("chg_abs" if r.unit == "time" else "chg_pct")


def _find_or_add(refs: list[Ref], mid: str, param: float | None = None) -> Ref:
    for r in refs:
        if r.kind == "base" and r.a == mid:
            return r
    r = Ref("base", mid, param=param)
    refs.append(r)
    return r


def _conditions(t: str, refs: list[Ref], a: Analysis) -> list[Cond]:
    conds: list[Cond] = []

    def add(c: Cond) -> None:
        if not any(x.ref == c.ref and x.measure == c.measure and x.op == c.op for x in conds):
            conds.append(c)

    t = t.replace("drop size", "dropsize")
    # special shapes first, then blank them so the generic patterns don't read them twice
    specials = [
        (r"secondary(?: sales)? (?:are |is )?less than (\d+)% of primary", lambda m: ("secondary_sales/primary_sales", "cur", "<", float(m.group(1)))),
        (r"secondary(?: sales)? (?:are |is )?more than (\d+)% of primary", lambda m: ("secondary_sales/primary_sales", "cur", ">", float(m.group(1)))),
        (r"secondary(?: sales)? is higher than primary by more than (\d+)%", lambda m: ("secondary_sales/primary_sales", "cur", ">", 100 + float(m.group(1)))),
        (r"less than (\d+)% of (?:their )?outlets billed", lambda m: ("ubo/visited_outlets", "cur", "<", float(m.group(1)))),
        (r"fewer than (\d+) calls on more than (\d+)", lambda m: (f"low_call_days@{m.group(1)}", "cur", ">", float(m.group(2)))),
        (r"days with at least \d+ calls", lambda m: (None, "", "", 0.0)),
        (r"\b(?:falling|declining|dropping|shrinking) (ubo|pc%?|coverage|penetration|sales|drop ?size|tc)\b",
         lambda m: (_key(refs, _alias(m.group(1))), "chg", "<", 0.0)),
        (r"\b(?:rising|growing|increasing) (ubo|pc%?|coverage|penetration|sales|drop ?size|tc)\b(?! faster)",
         lambda m: (_key(refs, _alias(m.group(1))), "chg", ">", 0.0)),
        (r"after \d{1,2}(?::\d\d)?\s*(?:am|pm) on more than (\d+) days", lambda m: (_key(refs, "late_start_days"), "cur", ">", float(m.group(1)))),
        (r"before \d{1,2}(?::\d\d)?\s*(?:am|pm) on more than (\d+)% of days", lambda m: (_key(refs, "early_end_days"), "cur", ">", float(m.group(1)))),
        (r"tour plan on fewer than (\d+)% of days|fewer than (\d+)% of days", lambda m: (_key(refs, "tour_plan_adherence_pct"), "cur", "<", float(m.group(1) or m.group(2)))),
        (r"more than (\d+)% (?:of )?reps missing", lambda m: ("working_reps", "vs_avg", "<", -float(m.group(1)))),
        (r"(?:lost|losing) more outlets than (?:we |they )?added", lambda m: ("inactive_outlets-new_outlets", "cur", ">", 0.0)),
        (r"(\w+) (?:is |are )?growing faster than (\w+)", lambda m: (f"faster:{m.group(1)}|{m.group(2)}", "chg_pct", ">", 0.0)),
        (r"more than (\d+)% below (?:their|its) average", lambda m: (_key(refs, None), "vs_avg", "<", -float(m.group(1)))),
        (r"(\d+)% (?:or more )?below (?:their|its) average", lambda m: (_key(refs, None), "vs_avg", "<", -float(m.group(1)))),
        (r"high sales growth \(above (\d+)%\)|sales growth (?:of |above |over )(?:more than )?(\d+)%", lambda m: (_key(refs, "secondary_sales"), "chg_pct", ">", float(m.group(1) or m.group(2)))),
    ]
    for pat, make in specials:
        for m in re.finditer(pat, t):
            ref, measure, op, val = make(m)
            if ref is None:
                t = _blank(t, *m.span())
                continue
            if measure == "chg":
                measure = _chg(next(r for r in refs if r.key == ref))
            if ref == "inactive_outlets-new_outlets":
                for mid in ("inactive_outlets", "new_outlets"):
                    _find_or_add(refs, mid)
                if not any(r.key == ref for r in refs):
                    refs.append(Ref("gap", "inactive_outlets", "new_outlets", label="Outlets lost minus outlets added"))
            if ref.startswith("low_call_days"):
                _find_or_add(refs, "low_call_days", float(ref.split("@")[1]))
            add(Cond(ref, measure, op, val, text=m.group(0)))
            t = _blank(t, *m.span())
    clauses = re.split(r"[.;]\s+|,\s+(?=(?:but|and|while|where|whose|with|only|which|or|then|along)\b)|\s+(?:but|while|whereas)\s+", t)
    for i, cl in enumerate(clauses):
        pre = bool(re.search(r"\b(only|among|excluding|serving|who worked|with at least|with more than|that sold)\b", cl))
        for m in re.finditer(r"(" + FALL + r")\s+or\s+(?:remained\s+|stayed\s+)?flat|flat\s+or\s+(?:" + FALL + r")|did not grow|"
                             r"didn't grow|no growth|(?:remained|stayed) flat", cl):
            r = _ref_for(cl[:m.start() + 1] or cl, refs) or (refs[-1] if refs else None)
            if r:
                op = "flat" if re.match(r"(?:remained|stayed) flat", m.group(0)) else "le_flat"
                add(Cond(r.key, _chg(r), op, FLAT_PCT, text=f"{r.name()} {'stayed flat' if op == 'flat' else 'declined or stayed flat'} "
                                                           f"(flat = within ±{FLAT_PCT:g}%)"))
            cl = _blank(cl, *m.span())
        pats = [r"(?P<verb>" + GROW + "|" + FALL + r")\s+(?:by\s+)?(?:of\s+)?(?P<cmp>" + _CMPW + r")\s+" + _NUM +
                r"\s*(?P<unit>percentage points|points|pts|pp|percent|%|minutes|mins)",
                r"(?:a\s+)?(?P<verb>drop|fall|decline|growth|increase|rise)\s+of\s+(?:(?P<cmp>" + _CMPW + r")\s+)?" + _NUM +
                r"\s*(?P<unit>percentage points|points|pts|percent|%)",
                r"(?P<verb>" + GROW + "|" + FALL + r")\s+" + _NUM + r"\s*(?P<unit>percentage points|points|pts|percent|%)",
                _NUM + r"\s*(?P<unit>%)\s*(?P<cmp>more than|less than)\s+(?P<verb>" + GROW + "|" + FALL + r")",
                r"(?P<verb>" + GROW + "|" + FALL + r")\s+(?:\S+\s+){1,4}?by\s+(?P<cmp>" + _CMPW + r")\s+" + _NUM +
                r"\s*(?P<unit>percentage points|points|pts|percent|%)",
                r"(?P<verb>moved later|later|moved earlier|earlier)\s+by\s+(?P<cmp>" + _CMPW + r")\s+" + _NUM + r"\s*(?P<unit>minutes|mins)"]
        used: list[tuple[int, int]] = []
        for p in pats:
            for m in re.finditer(p, cl):
                if any(m.start() < e and m.end() > s for s, e in used):
                    continue
                used.append(m.span())
                gd = m.groupdict()
                num = float(re.search(_NUM, m.group(0)).group(1))
                verb = gd.get("verb") or ""
                down = bool(re.fullmatch(FALL, verb)) or verb in ("drop", "fall", "decline", "moved earlier", "earlier")
                cmp_ = OPS.get(gd.get("cmp") or "more than", ">")
                unit = gd.get("unit") or "%"
                r = _ref_for(cl[:m.end()], refs) or _ref_for(cl, refs) or (a.rank or (refs[0] if refs else None))
                if not r:
                    continue
                measure = "chg_pts" if unit.startswith(("point", "percentage p", "pt", "pp")) else "chg_pct"
                if a.compare_kind == "avg":
                    measure = "vs_avg"
                if unit.startswith("min"):
                    measure, num = "chg_abs", num * 60
                op, val = (FLIP[cmp_], -num) if down else (cmp_, num)
                if unit.startswith("min") and verb in ("moved later", "later"):
                    op, val = cmp_, num
                add(Cond(r.key, measure, op, val, text=f"{r.name()} {verb} by {(gd.get('cmp') or '').strip()} {num:g}"
                                                       f"{'%' if unit == '%' else ' ' + unit}".replace("  ", " ")))
        cl3 = cl
        for s, e in used:
            cl3 = _blank(cl3, s, e)
        for m in re.finditer(r"(?P<cmp>" + _CMPW + r")\s+(?:rs\s*)?" + _NUM + r"(?:\s*(?P<unit>lakhs?|lacs?|crores?|cr|k|thousand|%|percent)\b)?"
                             r"(?:\s+(?P<what>total calls|calls a day|calls per day|calls|outlets|reps|working days|days))?", cl3):
            num = _amount(float(m.group(2)), m.group("unit"))
            cmp_ = OPS[m.group("cmp")]
            what = m.group("what") or ""
            after = cl3[m.end():m.end() + 40]
            r = None
            if what in ("calls a day", "calls per day") or re.match(r"\s*(?:calls )?(?:a|per) day", after):
                r = _find_or_add(refs, "calls_per_day")
            elif what in ("total calls", "calls"):
                r = _find_or_add(refs, "tc")
            elif what == "outlets":
                r = _find_or_add(refs, "active_outlets" if re.search(r"in total|outlet base", after + cl3) else "ubo")
            elif what == "reps":
                r = _find_or_add(refs, "working_reps")
            elif what in ("days", "working days") and re.search(r"leave|absent", cl3):
                r = _find_or_add(refs, "leave_absent_days")
            elif what in ("days", "working days"):
                r = _find_or_add(refs, "market_days")
            elif re.search(r"of calls into orders|calls into orders", after + cl3):
                r = _find_or_add(refs, "pc_pct")
            if r is None:
                r = _ref_for(cl3[:m.start()], refs) or _ref_for(cl3, refs)
            if r is None:
                continue
            if r.unit == "percent" and (m.group("unit") or "") not in ("%", "percent") and r.kind == "base" and \
                    r.a not in ("pc_pct", "coverage_pct", "penetration_pct", "ubo_pct", "tour_plan_adherence_pct"):
                continue
            measure = "cur"
            tail = cl3[m.end():m.end() + 45]
            if a.compare is not None and (re.search(r"earlier period|before that|in the (?:\d+ )?(?:days|weeks|months) before|in the previous",
                                                    tail + " " + cl3[:m.start()]) or _is_compare_period(tail, a)):
                measure = "prev"
            add(Cond(r.key, measure, cmp_, num, pre=pre or bool(re.search(r"\bonly\b", cl3)) and r is not a.rank,
                     text=f"{r.name()} {m.group('cmp')} " + (f"{num:g}%" if r.unit == "percent" else fmt.value(num, r.unit))
                          + (" (earlier period)" if measure == "prev" else "")))
        if re.search(r"\bbilled nothing\b|\bno billing\b|\bzero billing\b", cl3):
            r = _find_or_add(refs, "primary_sales")
            add(Cond(r.key, "cur", "<=", 0.0, text="Billed nothing in the period"))
        if re.search(r"\b(where|which|whose|but|while|and|with|flag|highlight|list|identify|find|any|so)\b", cl3) or i > 0:
            for m in re.finditer(r"(?P<m>[a-z%][a-z% ]{1,45}?)\s+(?:has |have |still |also )?(?P<verb>grew|increased|went up|rose|"
                                 r"improved|declined|decreased|went down|dropped|fell|fall|slowed down)(?!\s+(?:by|more|of|or|\d))", cl3):
                r = _ref_for(m.group("m"), refs)
                if not r or any(c.ref == r.key and c.measure.startswith("chg") for c in conds) or re.search(r"all three", cl3):
                    continue
                up = m.group("verb") in ("grew", "increased", "went up", "rose", "improved")
                add(Cond(r.key, "vs_avg" if a.compare_kind == "avg" else _chg(r), ">" if up else "<", 0.0,
                         text=f"{r.name()} {'went up' if up else 'went down'}"))
        if re.search(r"\ball three (fell|declined|dropped|went down)|declined on all three|fell on all", cl3):
            for r in refs[:3]:
                add(Cond(r.key, _chg(r), "<", 0.0, text=f"{r.name()} went down"))
        if re.search(r"below the (?:zone|team|overall|company) average", cl3):
            r = _ref_for(cl3[:re.search(r"below the", cl3).start()], refs)
            if r:
                add(Cond(r.key, "vs_avg", "<", 0.0, text=f"{r.name()} below the average of the list"))
                a.notes.append("\"Below the zone average\" is checked against the average of all the reps in the list.")
        if re.search(r"weak on all three|weak on all", cl3):
            for r in refs[:3]:
                add(Cond(r.key, "vs_avg", "<", 0.0, text=f"{r.name()} below average"))
        if re.search(r"(?:penetration|coverage|pc%?|ubo ?%?) below (\d+)%", cl3) is None:
            pass
    if re.search(r"\bwhich of them (improved|grew|went up)\b", t) and a.compare and refs:
        r = a.rank or refs[0]
        add(Cond(r.key, _chg(r), ">", 0.0, text=f"{r.name()} improved"))
    for c in conds:
        if c.measure == "prev" and re.search(r"\bonly (?:brands|reps|distributors|regions|zones|categories)\b", t):
            c.pre = True
    return conds


def _alias(word: str) -> str:
    w = word.replace(" ", "")
    return {"ubo": "ubo", "pc": "pc", "pc%": "pc_pct", "coverage": "coverage_pct", "penetration": "penetration_pct",
            "sales": "secondary_sales", "dropsize": "drop_size", "tc": "tc"}.get(w, "secondary_sales")


def _key(refs: list[Ref], mid: str | None) -> str | None:
    if mid is None:
        return refs[0].key if refs else None
    for r in refs:
        if r.a == mid:
            return r.key
    r = Ref("base", mid)
    refs.append(r)
    return r.key


def _is_compare_period(tail: str, a: Analysis) -> bool:
    m = re.match(r"\s*(?:in|during|for)\s+(" + MONTHS + r"|last month|the previous \w+|the earlier period)\b", tail)
    if not m or not a.compare:
        return False
    hit = find_period_span(m.group(1), a.compare.end, 4)
    return bool(hit) and hit[0].start <= a.compare.start <= hit[0].end


# =========================================================================== the engine


@dataclass
class _Fetch:
    tag: str  # "<base key>|<period tag>|<split label>"
    mid: str
    param: float | None
    period: Period
    keys: list[str]
    filters: list[Filter]
    order: str | None = None
    key_in: Filter | None = None
    pair: tuple[Period, Period] | None = None  # both periods in one query, rows tagged by the key "_pp"


class AnalystError(Exception):
    """A name that doesn't exist (args[0] is the RawFilter), or nothing that can be measured (args[0] is None)."""


def _bkey(mid: str, param: float | None) -> str:
    return f"{mid}@{int(param)}" if param is not None else mid


def _variant(mid: str, param: float | None, saved: dict, dims: list[str]):
    if mid in PARAM_METRICS:
        v = with_param(mid, param)
        bad = [d for d in dims if not v.supports(d)]
        return (v, None) if not bad else (None, f"{METRICS[mid].label} can't be broken down by "
                                                f"{', '.join(DIM_LABELS.get(d, d).lower() for d in bad)} in Pulse, so I left it out.")
    v, note = pick_variant(mid, saved.get(mid), dims)
    bad = [d for d in dims if not v.supports(d)]
    if bad:
        return None, (f"{METRICS[mid].label} can't be broken down by {', '.join(DIM_LABELS.get(d, d).lower() for d in bad)} "
                      f"in Pulse, so I left it out.")
    return v, note


def _merge_filters(fs: list[Filter]) -> list[Filter]:
    out: dict[str, Filter] = {}
    for f in fs:
        if f.dim in out:
            g = out[f.dim]
            out[f.dim] = Filter(f.dim, ids=sorted(set(g.ids) | set(f.ids)), names=list(dict.fromkeys(g.names + f.names)),
                                label=f"{g.label} and {f.label}")
        else:
            out[f.dim] = f
    return list(out.values())


async def run_analysis(session, query, plan: Analysis, ctx, saved: dict, budget: float, resolve) -> dict:
    """Fetch, compute and compose one answer. `resolve(raw_filters)` returns an entities.Resolution."""
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    if plan.sections:
        answers: list[dict] = []
        for i, sec in enumerate(plan.sections):
            left = budget - (loop.time() - t0)
            answers.append(await run_analysis(session, query, sec, ctx, saved, max(5.0, left / max(1, len(plan.sections) - i)),
                                              resolve))
            ans = answers[-1]
            top = (ans.get("_top") or [None])[0]
            if i + 1 < len(plan.sections) and plan.sections[i + 1].chain and top and sec.entity:
                fid = top.get("id")
                plan.sections[i + 1].extra_filter = (Filter(sec.entity, ids=[int(fid)], label=top["label"])  # type: ignore[attr-defined]
                                                     if str(fid).lstrip("-").isdigit()
                                                     else Filter(sec.entity, names=[top["label"]], label=top["label"]))
        first = answers[0]
        sections = [{"title": plan.titles[i] if i < len(plan.titles) else f"Part {i + 1}", "summary": a["summary"],
                     "chart": a.get("chart"), "table": a.get("table"), "notes": a.get("notes", [])} for i, a in enumerate(answers)]
        how = dict(first.get("how", {}))
        how["constraints"] = [f"Part {i + 1}: {c}" for i, a in enumerate(answers) for c in a.get("how", {}).get("constraints", [])]
        how["sql"] = [s for a in answers for s in a.get("how", {}).get("sql", [])]
        how["queries"] = len(how["sql"])
        return {**first, "summary": " ".join(f"({i + 1}) {a['summary']}" for i, a in enumerate(answers)), "sections": sections,
                "chart": None, "table": None, "how": how,
                "notes": list(dict.fromkeys(n for a in answers for n in a.get("notes", []))), "headline": None}

    notes = list(plan.notes)
    _RANK_NOTES.clear()
    res = await resolve(plan.filters + plan.split)
    if res.unresolved:
        raise AnalystError(res.unresolved[0])
    notes += res.notes
    n_split = len(plan.split)
    split_f = res.filters[len(res.filters) - n_split:] if n_split else []
    scope = _merge_filters(res.filters[:len(res.filters) - n_split] if n_split else res.filters)
    extra = getattr(plan, "extra_filter", None)
    if extra:
        scope = _merge_filters(scope + [extra])
        notes.append(f"Looking inside {DIM_LABELS[extra.dim].lower()} {extra.label}, from the first part.")
    ent = plan.entity
    keys = ([ent] if ent else []) + [k for k in plan.extra_keys if k != ent]
    filter_dims = [f.dim for f in scope]

    refs = list(plan.refs)
    if plan.rank and plan.rank.key not in [r.key for r in refs]:
        refs.insert(0, plan.rank)
    bases: dict[str, tuple[str, float | None]] = {}
    for r in refs:
        for mid, param in r.bases():
            bases[_bkey(mid, param)] = (mid, param)
    for d in plan.drivers:
        if d in METRICS:
            bases.setdefault(d, (d, None))
            if d not in [r.key for r in refs]:
                refs.append(Ref("base", d))

    periods: list[tuple[str, Period]] = [("cur", plan.period)] + ([("prev", plan.compare)] if plan.compare else [])
    baseline = None
    if plan.compare_kind == "avg":
        if plan.baseline_grain == "month":
            bs, be = add_months(month_start(plan.period.start), -plan.baseline_n), month_start(plan.period.start) - timedelta(days=1)
        else:
            mon = plan.period.start - timedelta(days=plan.period.start.weekday())
            bs, be = mon - timedelta(days=7 * plan.baseline_n), mon - timedelta(days=1)
        baseline = Period(bs, be, f"average {plan.baseline_grain} of {_label(Period(bs, be, ''))}", "custom")
    grain_keys = keys + ([plan.grain] if plan.grain else []) + (["day"] if plan.weekday_split else [])

    variants: dict[str, Any] = {}
    for bk, (mid, param) in list(bases.items()):
        v, note = _variant(mid, param, saved, [k for k in keys] + filter_dims + [f.dim for f in split_f])
        if note and note not in notes:
            notes.append(note)
        if v is None:
            bases.pop(bk)
            continue
        variants[bk] = v
    refs = [r for r in refs if all(_bkey(m, p) in bases for m, p in r.bases())]
    if not refs or (plan.rank and plan.rank.key not in [r.key for r in refs] and plan.rank.kind == "base"):
        raise AnalystError(None, [n for n in notes if "can't be broken down" in n] or notes)
    rank = plan.rank if plan.rank and plan.rank.key in [r.key for r in refs] else (refs[0] if plan.top_n or plan.groups else None)
    sqls: list[str] = []
    data: dict[str, dict] = {}

    async def run(fetches: list[_Fetch]) -> None:
        built = []
        for f in fetches:
            b = build_metric(ctx.id, variants[_bkey(f.mid, f.param)], f.keys + (["_pp"] if f.pair else []), f.period, f.filters,
                             tz_offset_minutes=ctx.tz_offset, order=f.order, limit=500 if f.order else None, key_in=f.key_in,
                             pair=f.pair)
            sqls.append(b.sql)
            built.append((f, b.sql))
        left = max(5.0, budget - (loop.time() - t0))
        got = await asyncio.gather(*[query(session, sql, ctx.id, left) for _, sql in built], return_exceptions=True)
        for (f, _), r in zip(built, got):
            if isinstance(r, BaseException):
                if type(r).__name__ == "GuardViolation":
                    raise r
                notes.append(f"{METRICS[f.mid].label} for {_label(f.period)} didn't come back in time.")
                continue
            if f.pair:
                n = len(f.keys)
                bk, _, label = f.tag.split("|")
                for ptag in ("cur", "prev"):
                    data[f"{bk}|{ptag}|{label}"] = {"rows": [{**x, **{k: v for k, v in x.items() if k != f"k{n}"}}
                                                             for x in r if x.get(f"k{n}") == ptag], "keys": f.keys}
            else:
                data[f.tag] = {"rows": r, "keys": f.keys}

    split_runs = [(sf.label, _merge_filters(scope + [sf])) for sf in split_f] if split_f else [("", scope)]
    two_round = bool(ent) and bool(plan.top_n or plan.groups) and not plan.filter_first and not split_f and not plan.extra_keys
    if two_round and ent in ("state", "city"):
        # a city / state split of sales joins the outlet master in every query: one round is quicker. Only the
        # outlet-master snapshots (inactive outlets...) need the second round, because thousands of cities exist.
        two_round = all(c.source in ("outlets", "outlets_created") for v in variants.values() for c in v.components.values())
    pre_conds = [c for c in plan.conds if c.pre]
    post = [c for c in plan.conds if not c.pre]

    def series_fetches(label: str, flt: list[Filter], key_in: Filter | None) -> list[_Fetch]:
        out = []
        for bk, (mid, param) in bases.items():
            if plan.grain or plan.weekday_split:
                out.append(_Fetch(f"{bk}|series|{label}", mid, param, plan.period, grain_keys, flt, key_in=key_in))
            if baseline:
                out.append(_Fetch(f"{bk}|base|{label}", mid, param, baseline, keys + [plan.baseline_grain], flt, key_in=key_in))
        return out

    if two_round:
        need = {_bkey(m, p) for r in refs if r.key == rank.key or r.key in {c.ref for c in pre_conds} for m, p in r.bases()}
        first = []
        for bk in need:
            mid, param = bases[bk]
            for ptag, per in periods:
                rank_cur = bk == _bkey(rank.a, rank.param) and ptag == "cur" and rank.kind == "base" and plan.rank_measure == "cur"
                first.append(_Fetch(f"{bk}|{ptag}|", mid, param, per, keys, scope, order=plan.top_dir if rank_cur else None))
        await run(first)
        members = _assemble(data, keys)
        _derive(members, refs, periods, {}, [""])
        pool = _apply(members, pre_conds, split_f)
        ordered = _ranked(pool, members, rank, plan)
        if plan.groups:
            universe = list(dict.fromkeys(ordered[:plan.groups[0][1]] + ordered[-plan.groups[1][1]:]))
        else:
            universe = ordered[:plan.top_n]
        notes += _pre_notes(plan, len(members), len(pool))
        if ent in ("state", "city"):
            names = [str(k[0]) for k in universe if k[0]]
            key_in = Filter(ent, names=names, label="the ranked list") if names else None
        else:
            ids = [int(k[0]) for k in universe if str(k[0]).lstrip("-").isdigit()]
            key_in = Filter(ent, ids=ids, label="the ranked list") if ids else None
        second = []
        for bk, (mid, param) in bases.items():
            missing = [(ptag, per) for ptag, per in periods if f"{bk}|{ptag}|" not in data]
            if len(missing) == 2 and _dated(variants[bk]) and key_in is not None:
                second.append(_Fetch(f"{bk}|pair|", mid, param, plan.period, keys, scope, key_in=key_in,
                                     pair=(plan.period, plan.compare)))
                continue
            for ptag, per in missing:
                second.append(_Fetch(f"{bk}|{ptag}|", mid, param, per, keys, scope, key_in=key_in))
        second += series_fetches("", scope, key_in)
        if plan.drill and refs:
            for mid, param in refs[0].bases()[:1]:
                for ptag, per in periods:
                    second.append(_Fetch(f"{_bkey(mid, param)}|drill-{ptag}|", mid, param, per, keys + [plan.drill], scope, key_in=key_in))
        if second:
            await run(second)
    else:
        fetches = []
        small = not keys or ent in ("zone", "region", "brand", "category")  # few members: both periods fit in one result
        for label, flt in split_runs:
            for bk, (mid, param) in bases.items():
                if len(periods) == 2 and small and _dated(variants[bk]):
                    fetches.append(_Fetch(f"{bk}|pair|{label}", mid, param, plan.period, keys, flt, pair=(plan.period, plan.compare)))
                    continue
                for ptag, per in periods:
                    fetches.append(_Fetch(f"{bk}|{ptag}|{label}", mid, param, per, keys, flt))
            fetches += series_fetches(label, flt, None)
        await run(fetches)
    members = _assemble(data, keys)
    test = [k for k, m in members.items() if re.match(r"(?i)^\s*test\b", m["label"])]
    for k in test:
        members.pop(k)  # demo / test records in the masters ("Test", "Test Region") are not real business
    for k in [k for k, m in members.items() if not any(v not in (None, 0) for v in m["vals"].values()) and not m["series"]]:
        members.pop(k)  # ids with no name and no numbers (deleted masters) are noise
    if ent in ("category", "sku", "brand"):
        members = _merge_same_names(members)
    _derive(members, refs, periods, _baseline_avgs(data, keys, plan, baseline) if baseline else {},
            [s for s, _ in split_runs])
    if plan.period.end >= ctx.today and plan.period.start <= ctx.today:
        notes.append("Today's data is still syncing from the field, so the current period is incomplete.")
    if two_round:
        universe = [k for k in universe if k in members]
    else:
        pool = _apply(members, pre_conds, split_f)
        notes += _pre_notes(plan, len(members), len(pool))
        universe = _apply({k: members[k] for k in pool}, post, split_f) if plan.filter_first else pool
        if plan.top_n and rank:
            universe = _ranked(universe, members, rank, plan)[:plan.top_n]
    flagged = _apply({k: members[k] for k in universe}, post, split_f) if post else []
    listing = universe
    if post and not plan.top_n and not plan.groups and not plan.grain:
        listing = flagged  # a filter, not a flag, when the question asks for a list
    if plan.sort:
        listing = _sorted(listing, members, plan.sort)
    elif not plan.top_n and listing and refs and not plan.grain:
        r0 = next((r for r in refs if any(c.ref == r.key for c in post)), refs[0])
        listing = sorted(listing, key=lambda k: _num_or(_val(members[k], r0.key, "cur"), -1e18), reverse=r0.higher_is_better)
    notes += list(dict.fromkeys(_RANK_NOTES))
    _RANK_NOTES.clear()
    ans = _compose(plan, ctx, members, universe, listing, flagged, refs, rank, baseline, split_f, scope, notes, post)
    ans["how"]["sql"] = sqls
    ans["how"]["queries"] = len(sqls)
    ans["_top"] = [{"id": members[k]["id"], "label": members[k]["label"]} for k in listing[:1]]
    return ans


def _dated(variant) -> bool:
    from .catalog import SOURCES
    return all(SOURCES[c.source].date_expr for c in variant.components.values())


def _merge_same_names(members: dict) -> dict:
    """A category that exists under two brands is one line: its totals are added up."""
    out: dict[tuple, dict] = {}
    seen: dict[str, tuple] = {}
    for k, m in members.items():
        name = m["label"].strip().lower()
        if name not in seen:
            seen[name] = k
            out[k] = m
            continue
        first = out[seen[name]]
        for tag, v in m["vals"].items():
            mid = tag.split("|")[0].split("@")[0]
            if mid in METRICS and METRICS[mid].unit in ("currency", "count") and METRICS[mid].variants[0].components.get("v"):
                first["vals"][tag] = (first["vals"].get(tag) or 0.0) + (v or 0.0)
            else:
                first["vals"].setdefault(tag, v)
    return out


def _num_or(v, default):
    return default if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else v


def _assemble(data: dict, keys: list[str]) -> dict:
    """Rows of every fetch -> {entity key: {label, id, vals: {tag: v}, series: {tag: {bucket: v}}, drill: {...}}}."""
    members: dict[tuple, dict] = {}
    n = len(keys)

    def member(r: dict) -> tuple | None:
        k = tuple(r.get(f"k{i}") for i in range(n)) if n else ("total",)
        if n and k[0] in (None, 0, "", "0"):
            return None
        if k not in members:
            label = " · ".join(str(r.get(f"k{i}_name") or r.get(f"k{i}") or "Not mapped").strip() for i in range(n)) if n else "Total"
            members[k] = {"label": label, "id": k[0], "vals": {}, "series": {}, "drill": {}}
        return k

    for tag, d in data.items():
        bk, ptag, split = tag.split("|")
        for r in d["rows"]:
            if ptag in ("cur", "prev"):
                k = member(r)
                if k is not None:
                    members[k]["vals"][f"{bk}|{ptag}|{split}"] = _f(r.get("v"))
            elif ptag in ("series", "base") or ptag.startswith("drill-"):
                kk = tuple(r.get(f"k{i}") for i in range(n)) if n else ("total",)
                if n and kk[0] in (None, 0, "", "0"):
                    continue
                if kk not in members:
                    if ptag == "series":
                        member(r)
                    else:
                        continue
                bucket = r.get(f"k{n}")
                if ptag.startswith("drill-"):
                    name = str(r.get(f"k{n}_name") or bucket or "").strip()
                    members[kk]["drill"].setdefault(name, {})[ptag[6:]] = _f(r.get("v"))
                else:
                    members[kk]["series"].setdefault(f"{bk}|{ptag}|{split}", {})[str(bucket)] = _f(r.get("v"))
    return members


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _baseline_avgs(data: dict, keys: list[str], plan: Analysis, baseline: Period) -> dict:
    """Average per month / week over the baseline window, scaled to the current period's length for totals."""
    out = {}
    n = len(keys)
    for tag, d in data.items():
        bk, ptag, split = tag.split("|")
        if ptag != "base":
            continue
        sums: dict[tuple, float] = {}
        for r in d["rows"]:
            k = tuple(r.get(f"k{i}") for i in range(n)) if n else ("total",)
            sums[k] = sums.get(k, 0.0) + (_f(r.get("v")) or 0.0)
        additive = METRICS[bk.split("@")[0]].unit in ("currency", "count")
        for k, total in sums.items():
            per = total / max(1, plan.baseline_n)
            if additive:
                per = per * plan.period.days / (baseline.days / max(1, plan.baseline_n))
            out[(k, bk, split)] = per
    return out


def _val(m: dict, key: str, measure: str, split: str = "") -> float | None:
    return m.get("calc", {}).get((key, measure, split))


def _derive(members: dict, refs: list[Ref], periods, baseline_avgs: dict, splits: list[str]) -> None:
    totals: dict[tuple, float] = {}
    for sp in splits:
        for r in refs:
            if r.kind in ("share", "sharediff"):
                for mid in [r.a] + ([r.b] if r.b else []):
                    for ptag, _ in periods:
                        totals[(mid, ptag, sp)] = sum((m["vals"].get(f"{mid}|{ptag}|{sp}") or 0) for m in members.values())
    for k, m in members.items():
        calc = m.setdefault("calc", {})
        for sp in splits:
            for r in refs:
                for ptag, _ in periods:
                    calc[(r.key, ptag, sp)] = _ref_value(m, r, ptag, sp, totals)
                if r.kind == "base" and (k, r.key, sp) in baseline_avgs:
                    calc[(r.key, "prev", sp)] = baseline_avgs[(k, r.key, sp)]
                cur, prev = calc.get((r.key, "cur", sp)), calc.get((r.key, "prev", sp))
                if cur is not None and prev not in (None, 0):
                    calc[(r.key, "chg_pct", sp)] = (cur - prev) / abs(prev) * 100
                elif cur is not None and cur > 0 and prev == 0:
                    calc[(r.key, "chg_pct", sp)] = math.inf
                if cur is not None and prev is not None:
                    calc[(r.key, "chg_pts", sp)] = calc[(r.key, "chg_abs", sp)] = cur - prev
                if baseline_avgs and r.kind == "base" and cur is not None and prev:
                    calc[(r.key, "vs_avg", sp)] = (cur - prev) / abs(prev) * 100
    for r in refs:
        for sp in splits:
            vals = [v for v in (m["calc"].get((r.key, "cur", sp)) for m in members.values()) if v is not None]
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            for m in members.values():
                v = m["calc"].get((r.key, "cur", sp))
                if (r.key, "vs_avg", sp) not in m["calc"] and v is not None and mean:
                    m["calc"][(r.key, "vs_avg", sp)] = (v - mean) / abs(mean) * 100


def _ref_value(m: dict, r: Ref, ptag: str, sp: str, totals: dict) -> float | None:
    def base(mid: str, param: float | None = None) -> float | None:
        v = m["vals"].get(f"{_bkey(mid, param)}|{ptag}|{sp}")
        if v is None and m["vals"] and METRICS[mid].unit in ("currency", "count") and                 any(k.endswith(f"|{ptag}|{sp}") for k in m["vals"]) and METRICS[mid].variants[0].components.get("v"):
            return 0.0  # the row exists for other metrics in this period, so no rows here means none (zero sales, zero calls)
        return v
    if r.kind == "base":
        return base(r.a, r.param)
    if r.kind == "dayshare":
        a, b = base(r.a, r.param), base("market_days")
        return None if a is None or not b else a / b * 100
    a, b = base(r.a), (base(r.b) if r.b else None)
    if r.kind == "ratio":
        return None if a is None or not b else a / b * 100
    if r.kind == "per":
        return None if a is None or not b else a / b
    if r.kind == "gap":
        return None if a is None and b is None else (a or 0) - (b or 0)
    if r.kind == "share":
        tot = totals.get((r.a, ptag, sp))
        return None if a is None or not tot else a / tot * 100
    if r.kind == "sharediff":
        ta, tb = totals.get((r.a, ptag, sp)), totals.get((r.b, ptag, sp))
        return None if a is None or b is None or not ta or not tb else a / ta * 100 - b / tb * 100
    return None


def _check(m: dict, c: Cond, splits: list[Filter] | None = None) -> bool:
    if c.ref.startswith("faster:"):
        if not splits or len(splits) < 2:
            return False
        names = c.ref[7:].split("|")
        labels = [s.label for s in splits]
        la = next((x for x in labels if x.lower().startswith(names[0].lower())), labels[0])
        lb = next((x for x in labels if x.lower().startswith(names[1].lower())), labels[1])
        key = next((k[0] for k in m.get("calc", {}) if k[1] == "chg_pct"), None)
        va, vb = (m["calc"].get((key, "chg_pct", la)), m["calc"].get((key, "chg_pct", lb))) if key else (None, None)
        return va is not None and vb is not None and va > vb
    v = _val(m, c.ref, c.measure)
    if v is None:
        return c.measure == "cur" and c.op in ("<=", "<") and (c.value or 0) <= 0  # no rows at all = nothing billed
    x = c.value or 0.0
    return {">": v > x, ">=": v >= x, "<": v < x, "<=": v <= x, "le_flat": v <= x, "ge_flat": v >= -x,
            "flat": abs(v) <= x}.get(c.op, False)


def _apply(members: dict, conds: list[Cond], splits: list[Filter] | None = None) -> list:
    return [k for k, m in members.items() if all(_check(m, c, splits) for c in conds)]


def _pre_notes(plan: Analysis, before: int, after: int) -> list[str]:
    pre = [c for c in plan.conds if c.pre]
    if not pre:
        return []
    return [f"Only {DIM_LABELS.get(plan.entity or '', 'item').lower()}s with " + "; ".join(c.text for c in pre) +
            f" were considered ({after} of {before})."]


def _rank_value(m: dict | None, rank: Ref, plan: Analysis) -> float:
    worst = -1e18 if plan.top_dir == "desc" else 1e18
    if m is None:
        return worst
    if plan.rank_measure == "gapchg" and plan.sort:
        a_, b_ = plan.sort[1].split("|")
        va, vb = _val(m, a_, "chg_pct"), _val(m, b_, "chg_pct")
        v = None if va is None or vb is None else va - vb
    elif plan.rank_measure == "abs":
        v = _val(m, rank.key, "cur")
        v = abs(v) if v is not None else None
    else:
        v = _val(m, rank.key, plan.rank_measure)
    if plan.rank_measure in ("chg_pct", "vs_avg") and isinstance(v, float) and math.isinf(v):
        return worst  # nothing in the earlier period: "new", not the biggest growth
    return _num_or(v, worst)


def _ranked(keys: list, members: dict, rank: Ref, plan: Analysis) -> list:
    if plan.rank_measure == "chg_pct":
        # growth on a tiny base (₹500 to ₹50,000) isn't a real top performer: needs at least 10% of the typical earlier value
        prev = sorted(v for v in (_val(members[k], rank.key, "prev") for k in keys if k in members) if v)
        if len(prev) >= 5:
            floor = prev[len(prev) // 2] * 0.1
            small = [k for k in keys if (_val(members.get(k, {}), rank.key, "prev") or 0) < floor]
            if small:
                keys = [k for k in keys if k not in small]
                _RANK_NOTES.append(f"{len(small)} with very little in the earlier period (under {fmt.value(floor, rank.unit)}) "
                                   "were left out of the growth ranking.")
    return sorted(keys, key=lambda k: _rank_value(members.get(k), rank, plan), reverse=plan.top_dir == "desc")


_RANK_NOTES: list[str] = []


def _sorted(keys: list, members: dict, sort: tuple[str, str, str]) -> list:
    key, measure, direction = sort
    worst = -1e18 if direction == "desc" else 1e18
    if key == "gapchg":
        a_, b_ = measure.split("|")

        def g(k):
            va, vb = _val(members[k], a_, "chg_pct"), _val(members[k], b_, "chg_pct")
            return worst if va is None or vb is None else _num_or(va - vb, worst)
        return sorted(keys, key=g, reverse=direction == "desc")
    return sorted(keys, key=lambda k: _num_or(_val(members[k], key, measure), worst), reverse=direction == "desc")


# --------------------------------------------------------------------------- composing the answer


def _fmt_chg(v: float | None, measure: str, unit: str) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and math.isinf(v):
        return "new"
    if measure == "chg_pts":
        return f"{'+' if v >= 0 else '−'}{abs(v):.1f} pts"
    if measure == "chg_abs" and unit == "time":
        mins = round(v / 60)
        return "same" if mins == 0 else f"{abs(mins)} min {'later' if mins > 0 else 'earlier'}"
    if measure == "chg_abs":
        return ("+" if v >= 0 else "−") + fmt.value(abs(v), unit)
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f}%"


def _measure(r: Ref, baseline) -> str:
    return "vs_avg" if baseline is not None and r.kind == "base" else _chg(r)


def _compose(plan, ctx, members, universe, listing, flagged, refs, rank, baseline, split_f, scope, notes, post) -> dict:
    ent = plan.entity
    word = DIM_LABELS.get(ent, "").lower() if ent else ""
    if word in ("sku",):
        word = "SKU"
    plural = (word[:-1] + "ies" if word.endswith("y") else word + "s") if word else ""
    cur_p, prev_p = plan.period, plan.compare or baseline
    cur_l = _label(cur_p)
    prev_l = (baseline.label if baseline else _label(prev_p)) if prev_p else ""
    scope_l = ", ".join(f"{DIM_LABELS.get(f.dim, f.dim)} {f.label}" for f in scope)
    has_prev = prev_p is not None
    show = [r for r in plan.refs if r.key in [x.key for x in refs]] or list(refs)
    if rank and rank.key not in [r.key for r in show]:
        show = [rank] + show
    for d in plan.drivers:
        if d not in [r.key for r in show] and d in [r.key for r in refs]:
            show.append(next(r for r in refs if r.key == d))
    splits = [s.label for s in split_f] if split_f else [""]

    # ---- the table: entity, each metric for each period, the change, and the extra columns asked for
    cols = [{"key": "label", "label": " · ".join(DIM_LABELS.get(k, k) for k in ([ent] if ent else []) + plan.extra_keys) or "Total"}]
    many = len(show) * (3 if has_prev else 1) * len(splits) > 10
    for sp in splits:
        for r in show:
            nm = r.name() + (f" · {sp}" if sp else "")
            cols.append({"key": f"{r.key}|cur|{sp}", "label": nm + (f" ({cur_l})" if has_prev and not many else ""), "unit": r.unit})
            if has_prev:
                if not many:
                    cols.append({"key": f"{r.key}|prev|{sp}", "label": nm + (" (average)" if baseline else f" ({prev_l})"), "unit": r.unit})
                cols.append({"key": f"{r.key}|chg|{sp}", "label": ("Change" if len(show) == 1 and not sp else f"Δ {r.short()}")
                             + (f" · {sp}" if sp and len(show) == 1 else "")})
    if plan.consecutive:
        cols.append({"key": "streak", "label": f"Longest fall ({plan.grain}s in a row)", "unit": "count"})
    if plan.drivers:
        cols.append({"key": "driver", "label": "Main driver"})
    if plan.drill:
        cols.append({"key": "drill", "label": f"Declined in ({DIM_LABELS[plan.drill].lower()})"})
    if plan.contribution:
        cols.append({"key": "contrib", "label": "Share of total change", "unit": "percent"})
    if plan.weekday_split and split_f and not ent:
        rows_ws = []
        m0 = next(iter(members.values()), None)
        for sp in splits:
            s_ = (m0["series"].get(f"{_bkey(refs[0].a, refs[0].param)}|series|{sp}") if m0 else None) or {}
            wk, sun = _weekday({"series": {f"{_bkey(refs[0].a, refs[0].param)}|series|": s_}}, refs[0])
            rows_ws.append({"label": sp, "wk": wk, "sun": sun})
        listing = []
        members_ws = rows_ws
    else:
        members_ws = None
    if plan.weekday_split:
        u = refs[0].unit
        cols += [{"key": "wk", "label": "Mon–Sat average per day", "unit": u}, {"key": "sun", "label": "Sunday average per day", "unit": u},
                 {"key": "sunshare", "label": "Sunday share of the week", "unit": "percent"}]
    if post and (plan.top_n or plan.groups or not listing) and not plan.filter_first:
        cols.append({"key": "match", "label": "Matches"})
    if plan.diagnose:
        cols.append({"key": "weak", "label": "Weakest vs average"})

    total_change = None
    if plan.contribution and rank:
        total_change = sum(d for d in (_val(m, rank.key, "chg_abs") for m in members.values()) if d is not None)
    rows = []
    shown = listing
    if not listing and post and universe:
        shown = _closest(members, universe, post)[:15]  # nothing matches: show the nearest rows, so the numbers are visible
    for k in shown[:200]:
        m = members[k]
        row: dict[str, Any] = {"label": m["label"], "id": m["id"]}
        for sp in splits:
            for r in show:
                row[f"{r.key}|cur|{sp}"] = _val(m, r.key, "cur", sp)
                if has_prev:
                    row[f"{r.key}|prev|{sp}"] = _val(m, r.key, "prev", sp)
                    ms = _measure(r, baseline)
                    row[f"{r.key}|chg|{sp}"] = _fmt_chg(_val(m, r.key, ms, sp), ms, r.unit)
        if plan.consecutive:
            row["streak"] = _streak(m, show[0])
        if plan.drivers:
            row["driver"] = _driver(m, plan.drivers, rank)
        if plan.drill:
            row["drill"] = _drill_text(m)
        if plan.contribution and total_change:
            d = _val(m, rank.key, "chg_abs")
            row["contrib"] = None if d is None else d / total_change * 100
        if plan.weekday_split:
            wk, sun = _weekday(m, refs[0])
            row["wk"], row["sun"] = wk, sun
            row["sunshare"] = None if wk is None or sun is None or (wk * 6 + sun) == 0 else sun / (wk * 6 + sun) * 100
        if post and (plan.top_n or plan.groups or not listing) and not plan.filter_first:
            row["match"] = "✓" if k in flagged else ""
        if plan.diagnose:
            row["weak"] = _weakest(m, show)
        rows.append(row)
    if members_ws is not None:
        rows = [{"label": r["label"], "wk": r["wk"], "sun": r["sun"],
                 "sunshare": None if r["wk"] is None or r["sun"] is None or (r["wk"] * 6 + r["sun"]) == 0
                 else r["sun"] / (r["wk"] * 6 + r["sun"]) * 100} for r in members_ws]
        cols = [cols[0]] + [c for c in cols if c["key"] in ("wk", "sun", "sunshare")]
        cols[0] = {"key": "label", "label": "Area"}
    if plan.composite and listing:
        order = {k: i for i, k in enumerate(_composite(members, listing, show, has_prev))}
        pairs = sorted(zip(listing[:200], rows), key=lambda p: order.get(p[0], 999))
        listing, rows = [p[0] for p in pairs], [p[1] for p in pairs]
    table = {"columns": cols, "rows": rows}

    # ---- chart
    chart, trend_text = None, ""
    if plan.grain and not plan.weekday_split:
        table, chart, trend_text = _trend_table(plan, members, listing, show, splits)
    elif has_prev and rows and len(rows) <= 15 and not split_f:
        r0 = rank if rank and rank.kind not in ("sharediff",) else show[0]
        chart = {"type": "bar", "series": [{"id": f"{r0.key}|prev|", "label": "Average" if baseline else prev_l, "unit": r0.unit},
                                           {"id": f"{r0.key}|cur|", "label": cur_l, "unit": r0.unit}],
                 "data": rows, "highlight": None, "x_label": DIM_LABELS.get(ent, "")}
    elif rows and len(rows) <= 15 and not split_f:
        r0 = rank or show[0]
        chart = {"type": "hbar", "series": [{"id": f"{r0.key}|cur|", "label": r0.name(), "unit": r0.unit}], "data": rows,
                 "highlight": None, "x_label": DIM_LABELS.get(ent, "")}
    if len(show) > 3 and chart and chart["type"] != "table" and not plan.grain:
        chart = {"type": "table", "series": [], "data": rows, "highlight": None, "x_label": ""}
    if plan.groups:
        table = _groups_table(plan, members, universe, show, rank)
        chart = None

    # ---- summary: the answer first
    def brief(k, which=None) -> str:
        m = members[k]
        bits = []
        if split_f:
            r = (which or show)[0]
            for sp in splits:
                ms = _measure(r, baseline)
                bits.append(f"{sp} {_fmt_chg(_val(m, r.key, ms, sp), ms, r.unit)}" if has_prev
                            else f"{sp} {fmt.value(_val(m, r.key, 'cur', sp), r.unit)}")
            return f"{m['label']} ({', '.join(bits)})"
        for r in (which or [x for x in show if any(c.ref == x.key for c in post)] or show[:2]):
            if has_prev:
                ms = _measure(r, baseline)
                bits.append(f"{_fmt_chg(_val(m, r.key, ms), ms, r.unit)} {r.short()}")
            else:
                bits.append(f"{r.short()} {fmt.value(_val(m, r.key, 'cur'), r.unit)}")
        return f"{m['label']} ({', '.join(bits)})" if bits else m["label"]

    what = plural or "results"
    if plan.top_n and rank:
        by = rank.name().lower() + (" growth" if plan.rank_measure in ("chg_pct", "chg_abs") else "")
        if plan.rank_measure == "gapchg":
            by = "the gap between primary and secondary growth"
        if rank.higher_is_better or plan.rank_measure != "cur":
            what = f"{'top' if plan.top_dir == 'desc' else 'bottom'} {plan.top_n} {plural} by {by}"
        else:
            what = f"{plan.top_n} {plural} with the {'most' if plan.top_dir == 'desc' else 'least'} {by}"
            if rank.unit == "time":
                what = f"{plan.top_n} {plural} with the {'latest' if plan.top_dir == 'desc' else 'earliest'} {by}"
    where = f" in {scope_l}" if scope_l else ""
    when = cur_l + (f" vs {prev_l}" if has_prev else "")
    parts: list[str] = []
    if plan.groups:
        parts.append(_groups_text(table, show, rank, cur_l))
    elif post and plan.top_n and not plan.filter_first:
        if flagged:
            parts.append(f"{len(flagged)} of the {what}{where} match ({when}): " + "; ".join(brief(k) for k in flagged[:6]) +
                         (f" and {len(flagged) - 6} more" if len(flagged) > 6 else "") + ".")
        else:
            parts.append(f"None of the {what}{where} match ({when}). Closest: " +
                         "; ".join(brief(k) for k in _closest(members, universe, post)[:3]) + ".")
        if len(universe) < plan.top_n:
            parts.append(f"Only {len(universe)} {plural} had data.")
    elif post and not plan.grain:
        if listing:
            lead = (what[:1].upper() + what[1:]) if plan.top_n else f"{len(listing)} {plural or 'items'}"
            parts.append(f"{lead}{where} match ({when}): " + "; ".join(brief(k) for k in listing[:6]) +
                         (f" and {len(listing) - 6} more" if len(listing) > 6 else "") + ".")
        else:
            close = _closest(members, list(members), post)[:3]
            parts.append(f"No {plural or 'items'}{where} match ({when})." + (" Closest: " + "; ".join(brief(k) for k in close) + "."
                                                                            if close else ""))
    elif plan.weekday_split and rows:
        best = max(rows, key=lambda r: r.get("sunshare") or -1)
        parts.append(f"{best['label']} depends most on Sundays: {fmt.percent(best.get('sunshare'))} of its weekly "
                     f"{refs[0].name().lower()} comes on Sundays ({cur_l}).")
    elif listing and not plan.grain:
        lead = (what[:1].upper() + what[1:]) if plan.top_n else (plural or "Results").capitalize()
        parts.append(f"{lead}{where} ({when}): " + "; ".join(brief(k, show[:2]) for k in listing[:5]) +
                     (f" and {len(listing) - 5} more" if len(listing) > 5 else "") + ".")
    if trend_text:
        parts.append(trend_text)
    if plan.consecutive:
        hit = [members[k]["label"] for k in listing if (_streak(members[k], show[0]) or 0) >= plan.consecutive]
        parts.append(f"{len(hit)} fell {plan.consecutive} or more {plan.grain}s in a row" + (f": {', '.join(hit[:8])}." if hit else "."))
    if plan.extremes and has_prev and listing and not plan.grain:
        parts.append(_extremes_text(plan, members, listing, show, rank, refs))
    if plan.contribution and rows and total_change:
        top = max(rows, key=lambda r: r.get("contrib") or -1e9)
        parts.append(f"{top['label']} contributed the most: {fmt.percent(top.get('contrib'))} of the total change of "
                     f"{fmt.value(total_change, rank.unit)}.")
    if plan.composite and listing:
        parts.append(f"Best overall: {members[listing[0]]['label']}; weakest: {members[listing[-1]]['label']} (average rank "
                     f"across {', '.join(r.short() for r in show)}{', on the change vs ' + prev_l if has_prev else ''}).")
    if plan.drivers and rows:
        counts: dict[str, int] = {}
        for r in rows:
            if r.get("driver") and r["driver"] != "–":
                counts[r["driver"]] = counts.get(r["driver"], 0) + 1
        if counts:
            parts.append("Main driver of the change: " + ", ".join(f"{k} for {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])) + ".")
    if plan.diagnose and rows:
        parts.append(_diagnose_text(rows))
    if split_f and any(c.ref.startswith("faster:") for c in post):
        parts = [f"{split_f[0].label} is growing faster than {split_f[1].label} in {len(listing)} of {len(universe)} "
                 f"{plural or 'places'} ({when})" + (": " + "; ".join(brief(k) for k in listing[:8]) + "." if listing else
                                                     ". Everywhere else it grew slower: " +
                                                     "; ".join(brief(k) for k in universe[:6]) + ".")]
    if not parts:
        parts.append(f"I found no data for {what}{where} in {when}.")
    unsupported = [f"I couldn't use {u}: Pulse doesn't have that for {ctx.name}." for u in plan.unsupported]

    constraints = [f"Period: {cur_l}"]
    if has_prev:
        constraints.append(f"Compared with: {prev_l}")
    if scope_l:
        constraints.append(f"Filters: {scope_l}")
    if split_f:
        constraints.append("Side by side: " + " vs ".join(s.label for s in split_f))
    if ent:
        constraints.append(f"List of: {plural}" + (f" × {', '.join(DIM_LABELS[k].lower() for k in plan.extra_keys)}" if plan.extra_keys else ""))
    if plan.top_n and rank:
        constraints.append(f"Ranking: {what}" + (" (among the ones that match)" if plan.filter_first else ""))
    if plan.groups:
        constraints.append("Groups: " + " vs ".join(f"{'top' if d == 'desc' else 'bottom'} {n}" for d, n in plan.groups))
    constraints.append("Metrics: " + ", ".join(r.name() for r in show))
    for c in plan.conds:
        constraints.append(("Only: " if c.pre else "Condition: ") + c.text)
    if plan.grain:
        constraints.append(f"Trend by {plan.grain}")
    if any(c.op in ("le_flat", "flat") for c in plan.conds):
        notes.append(f"\"Flat\" means a change within ±{FLAT_PCT:g}%.")
    metrics_how = []
    for r in show:
        for mid, _ in r.bases():
            if mid in METRICS and mid not in [x["id"] for x in metrics_how]:
                metrics_how.append({"id": mid, "label": METRICS[mid].label, "formula": METRICS[mid].default.formula, "source": "Pulse"})
    has_rows = bool(rows) or bool(table and table.get("rows"))
    return {
        "kind": "data" if has_rows else "empty",
        "summary": " ".join(parts),
        "summary_by": "template",
        "headline": (f"{len(flagged)} match" if post and plan.top_n and not plan.filter_first else None),
        "chart": chart if has_rows else None,
        "table": table if has_rows else None,
        "formula_notes": [],
        "notes": list(dict.fromkeys(unsupported + notes)),
        "terms": [],
        "suggestions": [],
        "how": {"period": when, "filters": scope_l or "None",
                "metrics": [{k: v for k, v in x.items() if k != "id"} for x in metrics_how],
                "interpreted_by": "rules", "constraints": constraints},
        "analysis": True,
    }


def _extremes_text(plan, members, listing, show, rank, refs) -> str:
    r0 = rank or show[0]
    ms = _chg(r0)
    valid = [k for k in listing if _num_or(_val(members[k], r0.key, ms), None) is not None]
    if not valid:
        return ""
    if plan.share:
        key = f"share:{r0.a}"
        shares = [k for k in valid if _val(members[k], key, "chg_pts") is not None]
        if not shares:
            tot_c = sum(_val(members[k], r0.key, "cur") or 0 for k in members)
            tot_p = sum(_val(members[k], r0.key, "prev") or 0 for k in members)
            for k in valid:
                c, p = _val(members[k], r0.key, "cur"), _val(members[k], r0.key, "prev")
                if c is not None and p is not None and tot_c and tot_p:
                    members[k]["calc"][(key, "chg_pts", "")] = c / tot_c * 100 - p / tot_p * 100
            shares = [k for k in valid if _val(members[k], key, "chg_pts") is not None]
        if shares:
            hi = max(shares, key=lambda k: _val(members[k], key, "chg_pts"))
            lo = min(shares, key=lambda k: _val(members[k], key, "chg_pts"))
            return (f"{members[hi]['label']} gained the most share ({_fmt_chg(_val(members[hi], key, 'chg_pts'), 'chg_pts', 'percent')}); "
                    f"{members[lo]['label']} lost the most ({_fmt_chg(_val(members[lo], key, 'chg_pts'), 'chg_pts', 'percent')}).")
    hi = max(valid, key=lambda k: _val(members[k], r0.key, ms))
    lo = min(valid, key=lambda k: _val(members[k], r0.key, ms))
    return (f"Highest growth: {members[hi]['label']} ({_fmt_chg(_val(members[hi], r0.key, ms), ms, r0.unit)}); "
            f"lowest: {members[lo]['label']} ({_fmt_chg(_val(members[lo], r0.key, ms), ms, r0.unit)}).")


def _closest(members: dict, keys: list, conds: list[Cond]) -> list:
    """Rows nearest to meeting every condition (how far each condition is from passing, summed)."""
    def gap(k):
        total = 0.0
        for c in conds:
            v = _val(members[k], c.ref, c.measure)
            if v is None or (isinstance(v, float) and (math.isinf(v) or math.isnan(v))):
                total += 1e6
                continue
            x = c.value or 0.0
            if c.op in (">", ">="):
                total += max(0.0, x - v)
            elif c.op in ("<", "<=", "le_flat"):
                total += max(0.0, v - x)
            elif c.op == "flat":
                total += max(0.0, abs(v) - x)
        return total
    return sorted(keys, key=gap)


def _series(m: dict, r: Ref, split: str = "") -> dict:
    return m["series"].get(f"{_bkey(r.a, r.param)}|series|{split}") or \
        next((s for t, s in m["series"].items() if t.startswith(f"{_bkey(r.a, r.param)}|series|")), {})


def _streak(m: dict, r: Ref) -> int | None:
    s = _series(m, r)
    if not s:
        return None
    vals = [s[b] for b in sorted(s)]
    best = run = 0
    for a, b in zip(vals, vals[1:]):
        run = run + 1 if (a is not None and b is not None and b < a) else 0
        best = max(best, run)
    return best


def _weekday(m: dict, r: Ref) -> tuple[float | None, float | None]:
    wk, sun = [], []
    for b, v in _series(m, r).items():
        try:
            d = date.fromisoformat(str(b)[:10])
        except ValueError:
            continue
        (sun if d.weekday() == 6 else wk).append(v or 0.0)
    return (sum(wk) / len(wk) if wk else None), (sum(sun) / len(sun) if sun else None)


def _driver(m: dict, drivers: list[str], rank: Ref | None) -> str:
    best, size = "–", 0.0
    target = _val(m, rank.key, "chg_pct") if rank else None
    for d in drivers:
        c = _val(m, d, "chg_pts" if d in METRICS and METRICS[d].unit == "percent" else "chg_pct")
        if c is None or (isinstance(c, float) and math.isinf(c)):
            continue
        if target not in (None, 0) and (c > 0) != (target > 0):
            continue
        if abs(c) > size:
            best, size = (METRICS[d].label if d in METRICS else d), abs(c)
    return best


def _drill_text(m: dict) -> str:
    falls = []
    for name, v in m["drill"].items():
        c, p = v.get("cur"), v.get("prev")
        if p and c is not None and c < p:
            falls.append((name, (c - p) / p * 100))
    falls.sort(key=lambda x: x[1])
    return ", ".join(f"{n} ({x:+.0f}%)" for n, x in falls[:4]) or "none"


def _weakest(m: dict, refs: list[Ref]) -> str:
    worst, score = "", 0.0
    for r in refs:
        v = _val(m, r.key, "vs_avg")
        if v is None:
            continue
        s = v if r.higher_is_better else -v
        if s < score:
            worst, score = r.short(), s
    return f"{worst} ({score:+.0f}% vs average)" if worst else "–"


_ACTION = {"PC%": "low_pc_pct", "calls per rep per day": "fewer_calls", "avg. call start time": "late_start",
           "drop size": "low_drop_size", "coverage": "low_coverage", "UBO%": "ubo_down", "UBO": "ubo_down", "LPC": "low_lpc",
           "avg. calls per sales rep": "fewer_calls", "TC": "fewer_calls", "PC": "low_pc_pct"}


def _diagnose_text(rows: list[dict]) -> str:
    from .rca import playbook
    book = playbook()
    out = []
    for r in rows[:5]:
        weak = (r.get("weak") or "").split(" (")[0]
        if not weak or weak == "–":
            continue
        steps = book.get(_ACTION.get(weak, "low_pc_pct"), {}).get("actions", [])[:3]
        out.append(f"{r['label']} is weakest on {weak}. What to do: " + " ".join(f"({i + 1}) {s}" for i, s in enumerate(steps)))
    return " ".join(out)


def _composite(members: dict, keys: list, refs: list[Ref], has_prev: bool) -> list:
    score: dict = {k: 0.0 for k in keys}
    for r in refs:
        ms = _chg(r) if has_prev else "cur"
        vals = [(k, _num_or(_val(members[k], r.key, ms), None)) for k in keys]
        vals = sorted([(k, v) for k, v in vals if v is not None], key=lambda x: x[1], reverse=r.higher_is_better)
        for i, (k, _) in enumerate(vals):
            score[k] += i
    return sorted(keys, key=lambda k: score[k])


def _bucket_label(b: str, grain: str) -> str:
    try:
        if grain == "month":
            return date(int(b[:4]), int(b[4:6]), 1).strftime("%b %Y")
        d = date.fromisoformat(b[:10])
        return ("Week of " if grain == "week" else "") + d.strftime("%d %b").lstrip("0")
    except ValueError:
        return b


def _trend_table(plan, members, listing, refs, splits):
    r0 = refs[0]
    buckets: set[str] = set()
    for m in members.values():
        buckets |= set(_series(m, r0))
    order = sorted(buckets)
    text = ""
    if not plan.entity:
        m = next(iter(members.values()), None)
        rows = []
        for b in order:
            row = {"label": _bucket_label(b, plan.grain)}
            for sp in splits:
                for r in refs:
                    row[f"{r.key}|{sp}"] = (m["series"].get(f"{_bkey(r.a, r.param)}|series|{sp}", {}) if m else {}).get(b)
            rows.append(row)
        cols = [{"key": "label", "label": plan.grain.title()}] + [
            {"key": f"{r.key}|{sp}", "label": r.name() + (f" · {sp}" if sp else ""), "unit": r.unit} for sp in splits for r in refs]
        series = [{"id": c["key"], "label": c["label"], "unit": c["unit"]} for c in cols[1:]]
        units = {s["unit"] for s in series}
        chart = {"type": "bar" if len(units) == 1 and len(series) <= 4 else "table", "series": series[:4], "data": rows,
                 "highlight": None, "x_label": plan.grain.title()}
        k0 = f"{r0.key}|{splits[0]}"
        vals = [(r["label"], r.get(k0), o) for r, o in zip(rows, order) if r.get(k0) is not None]
        if len(vals) >= 2:
            best = max(vals, key=lambda x: x[1])
            text = (f"{r0.name()} by {plan.grain}: {fmt.value(vals[0][1], r0.unit)} in {vals[0][0]} to "
                    f"{fmt.value(vals[-1][1], r0.unit)} in {vals[-1][0]}; highest in {best[0]} ({fmt.value(best[1], r0.unit)}).")
            drops = [(vals[i][0], vals[i][1] - vals[i - 1][1], vals[i - 1][1], vals[i][2]) for i in range(1, len(vals))]
            worst = min(drops, key=lambda x: x[1])
            if worst[1] < 0 and worst[2]:
                text += f" The biggest drop was in {worst[0]} ({worst[1] / worst[2] * 100:+.1f}% on the {plan.grain} before)."
                hol = _holidays_in(worst[3], plan.grain)
                if hol:
                    text += f" Likely reason: {hol}."
            if len(splits) > 1:
                for r in refs[:2]:
                    a_ = [x.get(f"{r.key}|{splits[0]}") for x in rows if x.get(f"{r.key}|{splits[0]}") is not None]
                    b_ = [x.get(f"{r.key}|{splits[1]}") for x in rows if x.get(f"{r.key}|{splits[1]}") is not None]
                    if a_ and b_:
                        text += (f" {r.name()}: {splits[0]} averages {fmt.value(sum(a_) / len(a_), r.unit)}, "
                                 f"{splits[1]} {fmt.value(sum(b_) / len(b_), r.unit)}.")
        if plan.weekday_split is False and plan.conds:
            dips = [(r["label"], r.get(f"{c.ref}|")) for c in plan.conds if c.measure == "vs_avg" for r in rows]
            vals2 = [v for _, v in dips if v is not None]
            if vals2:
                mean = sum(vals2) / len(vals2)
                lim = mean * (1 + (plan.conds[0].value or 0) / 100)
                low = [lab for lab, v in dips if v is not None and v < lim]
                text += (f" Days below {fmt.value(lim, refs[0].unit)} (more than {abs(plan.conds[0].value or 0):g}% under the average of "
                         f"{fmt.value(mean, refs[0].unit)}): " + (", ".join(low) if low else "none") + ".")
        return {"columns": cols, "rows": rows}, chart, text
    rows = []
    for k in listing[:50]:
        m = members[k]
        row = {"label": m["label"], "id": m["id"]}
        s = _series(m, r0)
        for b in order:
            row[b] = s.get(b)
        vals = [s.get(b) for b in order if s.get(b) is not None]
        if len(vals) >= 2 and vals[0]:
            row["growth"] = f"{(vals[-1] - vals[0]) / abs(vals[0]) * 100:+.1f}%"
        if plan.consecutive:
            row["streak"] = _streak(m, r0)
        rows.append(row)
    cols = [{"key": "label", "label": DIM_LABELS.get(plan.entity, "")}] + [{"key": b, "label": _bucket_label(b, plan.grain), "unit": r0.unit}
                                                                         for b in order]
    cols.append({"key": "growth", "label": f"Change, first to last {plan.grain}"})
    if plan.consecutive:
        cols.append({"key": "streak", "label": f"Longest fall ({plan.grain}s in a row)", "unit": "count"})
    g = [(r["label"], float(r["growth"].replace("%", ""))) for r in rows if r.get("growth")]
    if g:
        hi, lo = max(g, key=lambda x: x[1]), min(g, key=lambda x: x[1])
        text = (f"{r0.name()} by {plan.grain} for {len(rows)} {DIM_LABELS.get(plan.entity, '').lower()}s. "
                f"Highest growth: {hi[0]} ({hi[1]:+.1f}%); lowest: {lo[0]} ({lo[1]:+.1f}%).")
    return {"columns": cols, "rows": rows}, None, text


def _holidays_in(bucket: str, grain: str) -> str:
    try:
        d = date.fromisoformat(bucket[:10])
    except ValueError:
        return ""
    span = Period(d, d + timedelta(days=6 if grain == "week" else 0), "")
    hs = indcal.holidays_in(span)
    return ", ".join(f"{h.name} ({h.when})" for h in hs) + (" fell in that week" if grain == "week" else " fell that day") if hs else ""


def _groups_table(plan, members, universe, refs, rank) -> dict:
    ordered = _ranked(list(universe), members, rank, plan)
    n1, n2 = plan.groups[0][1], plan.groups[1][1]
    a, b = ordered[:n1], (ordered[-n2:] if len(ordered) > n1 else [])
    first = "Top" if plan.groups[0][0] == "desc" else "Bottom"
    second = "Bottom" if first == "Top" else "Top"
    rows = []
    for name, ks in ((f"{first} {n1} by {rank.short()}", a), (f"{second} {n2} by {rank.short()}", b)):
        row = {"label": name}
        for r in refs:
            vals = [v for v in (_val(members[k], r.key, "cur") for k in ks) if v is not None]
            row[r.key] = sum(vals) / len(vals) if vals else None
        rows.append(row)
    cols = [{"key": "label", "label": "Group"}] + [{"key": r.key, "label": r.name() + " (average)", "unit": r.unit} for r in refs]
    return {"columns": cols, "rows": rows}


def _groups_text(table, refs, rank, cur_l) -> str:
    a, b = table["rows"]
    bits = [f"{r.short()} {fmt.value(a.get(r.key), r.unit)} vs {fmt.value(b.get(r.key), r.unit)}"
            for r in refs if r.key != rank.key and a.get(r.key) is not None and b.get(r.key) is not None]
    return (f"{a['label']} vs {b['label']} ({cur_l}): " + "; ".join(bits) + ".") if bits else f"I couldn't compare the two groups for {cur_l}."
