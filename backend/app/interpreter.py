"""Question -> Plan.

A fast rule-based parser handles the common shapes of sales questions in well
under a second. When it can't find a metric, the local LLM is asked for the
same Plan as JSON (see llm.py). Either way the plan only ever names catalog
metrics, dimensions and periods: it never contains SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import glossary
from .catalog import ALL_DIMS, GEO_DIMS, METRICS
from .timeparse import Period, find_period, find_period_span, preset

# --------------------------------------------------------------------------- plan


@dataclass
class RawFilter:
    text: str
    dim_hint: str | None = None


@dataclass
class Plan:
    intent: str = "data"  # data | glossary | help | clarify
    metrics: list[str] = field(default_factory=list)
    shape: str = "value"  # value | trend | breakdown | ranking | compare_periods | compare_members
    dims: list[str] = field(default_factory=list)
    grain: str | None = None
    period: Period | None = None
    period_explicit: bool = False
    compare_periods: list[Period] = field(default_factory=list)
    filters: list[RawFilter] = field(default_factory=list)
    members: list[RawFilter] = field(default_factory=list)
    order: str = "desc"
    limit: int | None = None
    extreme: bool = False
    show_ids: bool = False
    growth: bool = False
    placeholder_dim: str | None = None
    glossary_terms: list[str] = field(default_factory=list)
    list_dim: str | None = None  # intent "list": which master to list
    count_only: bool = False  # "how many ..."
    lookup_term: str | None = None  # intent "lookup": "what is Pure", "who is Ramesh"
    notes: list[str] = field(default_factory=list)
    source: str = "rules"
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "intent": self.intent, "metrics": self.metrics, "shape": self.shape, "dims": self.dims,
            "grain": self.grain, "period": self.period.to_dict() if self.period else None,
            "compare_periods": [p.to_dict() for p in self.compare_periods],
            "filters": [f.__dict__ for f in self.filters], "members": [m.__dict__ for m in self.members],
            "order": self.order, "limit": self.limit, "extreme": self.extreme, "growth": self.growth,
            "notes": self.notes, "source": self.source, "list_dim": self.list_dim, "count_only": self.count_only,
        }


# --------------------------------------------------------------------------- vocab

# (pattern, metric ids). Longest / most specific first. Matched spans are blanked
# so "active outlets" does not also count as an "outlets" breakdown.
METRIC_PATTERNS: list[tuple[str, list[str]]] = [
    (r"(sales )?on invoice net value|net invoice value|invoice net value", ["secondary_sales"]),
    (r"primary (sales )?invoice value|primary invoices?", ["primary_invoice_value"]),
    (r"secondary (sales )?invoice value|secondary invoices?", ["secondary_invoice_value"]),
    (r"total invoice value", ["primary_invoice_value", "secondary_invoice_value"]),
    (r"invoice value|bill(ing)? value", ["@invoice_value"]),
    (r"primary (sales )?(and|&|vs\.?|versus|v/s) secondary( sales)?", ["primary_sales", "secondary_sales"]),
    (r"secondary (sales )?(and|&|vs\.?|versus|v/s) primary( sales)?", ["secondary_sales", "primary_sales"]),
    (r"primary sales|primary billing|primary|\bsell[- ]?in\b", ["primary_sales"]),
    (r"secondary sales|secondary billing|secondary|\bsell[- ]?out\b", ["secondary_sales"]),
    (r"\btc (and|&|vs\.?|versus|v/s) pc\b|total calls? (and|&|vs\.?|versus) productive calls?", ["tc", "pc"]),
    (r"\bpc (and|&|vs\.?|versus|v/s) tc\b", ["pc", "tc"]),
    (r"ubo ?(percentage|%)|ubo%|billing percentage|eco ?%", ["ubo_pct"]),
    (r"\bubos?\b|unique bill(ing|ed) outlets?|billed outlets?|bill cut outlets?|\becos?\b", ["ubo"]),
    (r"productive call ?(percentage|%|percent)|\bpc ?%|\bpc percentage|strike rate|call productivity|productivity( %| percentage)?|hit rate", ["pc_pct"]),
    (r"(average|avg\.?) (number of )?calls? per (rep|salesman|dsr|person|user)|calls? per (rep|salesman|dsr)", ["avg_calls_per_rep"]),
    (r"((average|avg\.?) )?(first )?call start time|first call time|market start time", ["avg_call_start_time"]),
    (r"productive outlets?|\bupc\b|ordering outlets?", ["productive_outlets"]),
    (r"inactive outlets?|blocked outlets?|deactivated outlets?|closed outlets?", ["inactive_outlets"]),
    (r"active outlets?|active stores?|active retailers?|live outlets?", ["active_outlets"]),
    (r"visited outlets?|\butc\b|outlets? visited", ["visited_outlets"]),
    (r"outlet growth|new outlets?|outlet addition|outlets? added", ["new_outlets"]),
    (r"lines per call|\blpc\b|\blppc\b", ["lpc"]),
    (r"productive calls?|productive visits?|\bpc\b", ["pc"]),
    (r"total calls?|\btc\b|\bcalls?\b|\bvisits?\b", ["tc"]),
    (r"outlet coverage|coverage", ["coverage_pct"]),
    (r"penetration", ["penetration_pct"]),
    (r"order value|order booking|booked value", ["order_value"]),
    (r"\borders?\b", ["orders"]),
    (r"drop ?size|average bill(ing)? value|\babv\b|average order value|\baov\b", ["drop_size"]),
]

GENERIC_SALES = r"\bsales\b|\brevenue\b|\bbusiness\b|perform(ing|ance|er)|\bsold\b|\bselling\b|\bsale\b"

DIM_PATTERNS: list[tuple[str, str]] = [
    ("zone", r"\bzones?\b|zone-?wise|\bzonal\b"),
    ("region", r"\bregions?\b|region-?wise"),
    ("territory", r"\bterr?it+or(y|ies)\b|territory-?wise|\bhqs?\b"),
    ("state", r"\bstates?\b|state-?wise"),
    ("city", r"\bcit(y|ies)\b|city-?wise|\btowns?\b"),
    ("category", r"\bcategor(y|ies)\b|category-?wise|\bsegments?\b"),
    ("brand", r"\bbrands?\b|brand-?wise|\bdivisions?\b"),
    ("sku", r"\bskus?\b|sku-?wise|\bproducts?\b|\bitems?\b"),
    ("distributor", r"\bdistributors?\b|\bdbs?\b|\bstockists?\b|distributor-?wise"),
    ("outlet", r"\boutlets?\b|\bretailers?\b|\bstores?\b|\bshops?\b|\bcounters?\b|outlet-?wise|retailer-?wise"),
    ("rep", r"\breps?\b|\bsalesm[ae]n\b|\bdsrs?\b|\bfield users?\b|\bsales ?persons?\b|\bsales ?people\b|\bemployees?\b|\bisrs?\b"),
]

GRAIN_PATTERNS: list[tuple[str, str]] = [
    ("day", r"which day|which date|day-?wise|date-?wise|\bdaily\b|by day|per day|each day"),
    ("week", r"\bweekly\b|week-?wise|by week|per week|week on week|wow"),
    ("month", r"by month|month-?wise|\bmonthly\b|month over month|month on month|\bmom\b|per month|each month"),
]

PLACEHOLDER = re.compile(r"\b(xyz|abc|xxx|x y z|some|any|particular|specific|a given|<[^>]+>)\b", re.I)

STOP_AFTER_NAME = (
    r"(?=\s+(?:for|in|over|during|from|by|vs\.?|versus|and|with|on|last|this|previous|past|today|yesterday|mtd|ytd|qtd|lmtd|"
    r"month|week|quarter|year|till|to date|trend|compared?)\b|[?.,!]|$)"
)

GLOSSARY_Q = re.compile(
    r"^(what (is|are|does)|what's|define|definition of|meaning of|full form of|full-form of|expand|explain|"
    r"how (is|are|do you|does pulse|do we) (calculate|compute|measure|define)d?|formula (of|for)|how to calculate)\b",
    re.I,
)


def _normalise(q: str) -> str:
    t = q.lower().strip()
    t = t.replace("’", "'").replace("–", "-")
    t = re.sub(r"\bsales\s*reps?\b|\bsales\s*representatives?\b|\bsales\s*executives?\b|\bsales\s*officers?\b", " rep ", t)
    t = re.sub(r"\bv/s\b|\bvs\b\.?", " vs ", t)
    t = re.sub(r"\bproducts? categor(y|ies)\b", lambda m: "categor" + m.group(1), t)
    t = re.sub(r"\s+", " ", t)
    return t


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _find_metrics(t: str) -> tuple[list[tuple[int, str]], str]:
    found: list[tuple[int, str]] = []
    work = t
    for pattern, mids in METRIC_PATTERNS:
        for m in re.finditer(pattern, work):
            for i, mid in enumerate(mids):
                found.append((m.start() + i, mid))
            work = _blank(work, m.start(), m.end())
    # Field slang from the glossary ("strike rate", "bill cut outlets", "sell-in").
    for term in glossary.find_terms(work):
        if term.metric_id and term.metric_id in METRICS:
            for alias in sorted(term.aliases + (term.term.lower(),), key=len, reverse=True):
                m = re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", work)
                if m:
                    found.append((m.start(), term.metric_id))
                    work = _blank(work, m.start(), m.end())
                    break
    found.sort()
    ordered: list[tuple[int, str]] = []
    for pos, mid in found:
        if mid not in [x[1] for x in ordered]:
            ordered.append((pos, mid))
    return ordered, work


def _find_dims(t: str) -> list[tuple[int, str]]:
    out = []
    for dim, pattern in DIM_PATTERNS:
        m = re.search(pattern, t)
        if m:
            out.append((m.start(), dim))
    out.sort()
    return out


def _find_grain(t: str) -> str | None:
    for grain, pattern in GRAIN_PATTERNS:
        if re.search(pattern, t):
            return grain
    return None


_DIM_WORDS = r"(zone|region|territory|terittory|state|city|category|brand|sku|distributor|outlet|retailer|division|hq)"


def _extract_filters(t: str) -> tuple[list[RawFilter], str, str | None]:
    """Named filters: "for North East Zone", "in West region", "for Karnataka", "for SKU XYZ".

    Returns filters, text with the filter spans blanked, and a placeholder dim if the user
    typed a placeholder such as "xyz zone".
    """
    filters: list[RawFilter] = []
    placeholder_dim = None
    work = t
    # "<name> zone" / "<name> region" after for / in / of
    pat1 = re.compile(
        r"\b(?:for|in|of|from|within|across|under)\s+(?:the\s+)?(?P<name>(?!(?:in|of|from|within|across|under)\b)[a-z](?:(?!\b(?:in|of|from|within|across|under)\b)[a-z0-9&()\-/ ]){0,40}?)\s+"
        r"(?P<dim>" + _DIM_WORDS + r"(?:\s*/\s*" + _DIM_WORDS + r")*)s?\b(?:\s+product)?"
    )
    # "sku xyz" / "category pure" / "distributor abc traders"
    pat2 = re.compile(
        r"\b(?:for|in|of|from|under)\s+(?:the\s+)?(?P<dim>sku|brand|category|distributor|outlet|retailer|zone|region|territory|state|city)\s+"
        r"(?P<name>[a-z0-9][a-z0-9&()\-/ ]{0,40}?)" + STOP_AFTER_NAME
    )
    for pat in (pat1, pat2):
        for m in list(pat.finditer(work)):
            name = m.group("name").strip()
            dim_word = m.group("dim").split("/")[0].strip()
            dim = {"terittory": "territory", "retailer": "outlet", "division": "brand", "hq": "territory"}.get(dim_word, dim_word)
            name = re.sub(r"\b(product|products|all|the)\b", " ", name).strip()
            if name in ("all", "each", "every", "", "active", "inactive", "our", "my", "total", "available", "new", "by") \
                    or name.startswith("all "):
                continue
            if PLACEHOLDER.fullmatch(name) or PLACEHOLDER.search(name):
                placeholder_dim = placeholder_dim or dim
                work = _blank(work, m.start(), m.end())
                continue
            # A time phrase is not a place.
            if re.search(r"\b(last|this|month|week|year|quarter|days?|today|yesterday|mtd|ytd)\b", name):
                continue
            filters.append(RawFilter(name, dim))
            work = _blank(work, m.start(), m.end())
    # Bare names: "for Karnataka", "for Delhi", "in Mumbai"
    pat3 = re.compile(r"\b(?:for|in|of|from)\s+(?P<name>[a-z][a-z .&\-]{1,30}?)" + STOP_AFTER_NAME)
    for m in list(pat3.finditer(work)):
        name = m.group("name").strip(" .")
        if not name or re.search(
            r"\b(last|this|next|previous|past|month|week|year|quarter|days?|today|yesterday|mtd|ytd|qtd|lmtd|"
            r"for|in|of|from|all|each|every|my|our|the|a|an|me|us|total|sales|data|trend|time|period|rep|reps|zones?|regions?|"
            r"states?|cities|city|categor(y|ies)|brands?|skus?|outlets?|distributors?|territor(y|ies)|calls?|orders?|"
            r"coverage|penetration|ubo|tc|pc|percentage|value|products?|by|wise|half|first|second)\b", name):
            continue
        if PLACEHOLDER.search(name):
            placeholder_dim = placeholder_dim or "zone"
            work = _blank(work, m.start(), m.end())
            continue
        filters.append(RawFilter(name, None))
        work = _blank(work, m.start(), m.end())
    return filters, work, placeholder_dim


def _vs_members(t: str) -> list[RawFilter]:
    m = re.search(r"\b(?:compare\s+)?(?P<a>[a-z][a-z ]{1,25}?)\s+vs\s+(?P<b>[a-z][a-z ]{1,25}?)(?=\s+(?:sales|coverage|penetration|ubo|calls|orders|tc|pc|for|in|by|last|this|mtd|ytd|secondary|primary|"
                  r"productive|total|order|invoice|outlets?|drop|lines|active|visited)\b|[?.]|$)", t)
    if not m:
        return []
    a, b = m.group("a").strip(), m.group("b").strip()
    a = re.sub(r"^compare\s+", "", a)
    # Strip leading verbs and trailing dimension words
    a = re.sub(r"^(show|give|get|compare)( me)?\s+", "", a)
    if any(re.search(p, x) for x in (a, b) for p, _ in METRIC_PATTERNS) or any(
            re.search(r"\b(month|week|year|quarter|today|yesterday|mtd|ytd|lmtd)\b", x) for x in (a, b)):
        return []
    dim_hint = None
    for dim, pattern in DIM_PATTERNS:
        if re.search(pattern, a + " " + b):
            dim_hint = dim
    clean = lambda x: re.sub(_DIM_WORDS + r"s?\b", "", x).strip()
    return [RawFilter(clean(a), dim_hint), RawFilter(clean(b), dim_hint)]


def _limit(t: str) -> int | None:
    m = re.search(r"\b(top|bottom|lowest|highest|best|worst|least|first|last)\s+(\d{1,3})\b", t)
    if m and m.group(1) != "last":
        return int(m.group(2))
    m = re.search(r"\b(\d{1,3})\s+(lowest|highest|best|worst|top|bottom)\b", t)
    if m:
        return int(m.group(1))
    return None


# --------------------------------------------------------------------------- lists and lookups

PLURAL_DIMS = re.compile(
    r"\b(categories|brands|skus|products|items|zones|regions|territories|states|cities|towns|distributors|stockists|"
    r"dbs|outlets|retailers|stores|shops|reps|salesmen|dsrs|field users|employees)\b")
LIST_CUE = re.compile(
    r"^(what|which)\b|\blist\b|\bnames?\b|\bhow many\b|\bnumber of\b|\bno\.? of\b|\bcount\b|"
    r"^(show|give|tell|get|display|fetch|find)\b|\b(do|does) (we|i|you|the company) (have|sell|stock)\b|"
    r"\bwe (have|sell)\b|\bavailable\b|\bportfolio\b|\ball (the )?\w+\b|\bmapped\b|\bunder\b|\bbelong")
COUNT_Q = re.compile(r"\bhow many\b|\bnumber of\b|\bno\.? of\b|\bcount\b|\btotal (number|no|count)\b")
_PORTFOLIO = re.compile(r"\b(portfolio|product range|range of products|variants|pack sizes)\b")
_LOOKUP_Q = re.compile(
    r"^(?:(?:what|who) (?:is|was)|what's|who's|tell me (?:something )?about|define|explain|meaning of|"
    r"do you know)\s+(?:the\s+|a\s+|an\s+)?(?P<x>[a-z0-9][a-z0-9 &'.\-]{0,40}?)\s*[?.!]*$")


def _list_dim(t: str, rest: str) -> str | None:
    """The master the user wants listed or counted, if this is a list question."""
    if not LIST_CUE.search(t):
        return None
    if _PORTFOLIO.search(t):
        return "sku"
    dims = [d for _, d in _find_dims(rest)]
    if not dims:
        return None
    if not PLURAL_DIMS.search(t) and not COUNT_Q.search(t) and not re.search(r"\b(list|all|names?)\b", t):
        return None  # "which zone ..." without a metric is not a list request
    return dims[0]


def _lookup_term(t: str) -> str | None:
    """"what is Pure", "who is Ramesh Kumar": a single name to look up in the company's masters."""
    m = _LOOKUP_Q.match(t.strip())
    if not m:
        return None
    x = m.group("x").strip(" .'")
    if not x or len(x.split()) > 5 or re.search(
            r"\b(top|best|worst|highest|lowest|most|least|total|my|our|this|last|today|trend)\b", x):
        return None
    return x


# --------------------------------------------------------------------------- parse


def parse(question: str, today: date, fy_start_month: int = 4) -> Plan:
    t = _normalise(question)
    plan = Plan()

    # Glossary questions: "what is UBO", "full form of PC", "how is coverage calculated"
    if GLOSSARY_Q.search(t) and not re.search(r"\b(my|our|today|mtd|ytd|last|this|yesterday|show|trend)\b", t) \
            and not PLURAL_DIMS.search(t):
        terms = glossary.find_terms(t)
        if terms:
            plan.intent = "glossary"
            plan.glossary_terms = [x.term for x in terms]
            return plan

    if re.fullmatch(r"(hi|hello|hey|hii+|good (morning|afternoon|evening)|help|what can you do\??|thanks?( you)?)[!. ]*", t):
        plan.intent = "help"
        return plan

    # Period(s)
    compare_periods: list[Period] = []
    left = right = ""
    if re.search(r"this month vs last month|current month vs (last|previous) month|mtd vs lmtd|month on month comparison", t):
        compare_periods = [preset("mtd", today, fy_start_month), preset("last_month", today, fy_start_month)]
    elif re.search(r"this week vs last week", t):
        compare_periods = [preset("this_week", today, fy_start_month), preset("last_week", today, fy_start_month)]
    elif re.search(r"today vs yesterday", t):
        compare_periods = [preset("today", today, fy_start_month), preset("yesterday", today, fy_start_month)]
    elif re.search(r"\s(?:vs|versus|compared to|against)\s", t):
        # "23 Oct vs 22 Oct", "September vs October", "Q1 vs Q2"
        left, right = re.split(r"\s(?:vs|versus|compared to|against)\s", t, maxsplit=1)
        a, b = find_period(left, today, fy_start_month), find_period(right, today, fy_start_month)
        if a and b and a != b:
            compare_periods = [a, b]
    found_period = find_period_span(t, today, fy_start_month)
    period = found_period[0] if found_period else None
    plan.period_explicit = period is not None

    # Metrics (blanking their text)
    found, rest = _find_metrics(t)
    metrics = [mid for _, mid in found]
    if "@invoice_value" in metrics:
        i = metrics.index("@invoice_value")
        metrics[i] = "primary_invoice_value" if "primary" in t else "secondary_invoice_value"
        if "primary" not in t:
            plan.notes.append("\"Invoice value\" is read as secondary (distributor-to-outlet) invoice value including taxes.")
    if not metrics and re.search(GENERIC_SALES, rest):
        metrics = ["secondary_sales"]
        plan.notes.append("\"Sales\" is read as secondary sales (distributor-to-outlet billing). Ask for primary sales if you meant company-to-distributor billing.")
    metrics = [m for m in metrics if m in METRICS]
    # "orders" + "order value" etc: keep order but drop duplicates
    plan.metrics = list(dict.fromkeys(metrics))[:3]
    rest = re.sub(GENERIC_SALES, lambda m: " " * len(m.group(0)), rest)  # keep positions
    # The date phrase ("23rd October", "1-15 Oct", "week of 13 Oct") is not a place or a split
    if compare_periods:
        rest = re.sub(r"\b(vs|versus|compared to|against)\b", " ", rest)
        for side in (left, right) if left else ():
            hit = find_period_span(side, today, fy_start_month)
            if hit:
                off = t.find(side)
                rest = _blank(rest, off + hit[1][0], off + hit[1][1])
    elif found_period:
        rest = _blank(rest, *found_period[1])

    # Named filters and placeholders ("from zone to state" is a drill-down, not a place)
    rest = re.sub(r"\bfrom (zone|region|state|territory|category|brand|city) to (zone|region|state|territory|category|brand|city|sku|outlet|distributor)\b",
                  lambda m: f"     {m.group(1)}    {m.group(2)}", rest)
    filters, rest, placeholder_dim = _extract_filters(rest)
    plan.filters = filters
    plan.placeholder_dim = placeholder_dim

    # Lists from the masters: "what are the product categories", "SKUs in Pure", "how many distributors in North"
    if not plan.metrics and not compare_periods:
        list_dim = _list_dim(t, rest)
        if list_dim:
            plan.intent = "list"
            plan.list_dim = list_dim
            plan.count_only = bool(COUNT_Q.search(t))
            plan.placeholder_dim = None
            return plan
        term = _lookup_term(t)
        if term:
            plan.intent = "lookup"
            plan.lookup_term = term
            plan.filters = []
            return plan

    # Compare members: "North vs South"
    if not compare_periods and len(plan.metrics) <= 1:
        members = _vs_members(t)
        if members:
            plan.members = members
            plan.filters = [f for f in plan.filters if f.text not in {m.text for m in members}]

    # Dimensions and grain
    dims_found = [d for _, d in _find_dims(rest)]
    grain = _find_grain(t)
    # "region/zone" -> one of them
    if re.search(r"\b(region|zone|territory|state)\s*/\s*(region|zone|territory|state)\b", t) and len(dims_found) >= 2:
        dims_found = dims_found[:1]
    is_drill = bool(re.search(r"drill ?down|from (zone|region|state|territory|category|brand) to", t))
    if is_drill:
        m = re.search(r"from (\w+) to (\w+)", t)
        if m:
            order_dims = [d for w in (m.group(1), m.group(2)) for d, p in DIM_PATTERNS if re.fullmatch(p, w)]
            dims_found = order_dims or dims_found

    # Shape
    trendy = bool(re.search(r"\btrends?\b|trend analysis|over time|month over month|month on month|\bmom\b|growth trend", t))
    extreme_word = re.search(r"\b(highest|lowest|maximum|minimum|max|min|most|least|best|worst|top|bottom|weakest|strongest)\b", t)
    asc = bool(re.search(r"\b(lowest|minimum|min|least|worst|bottom|weakest|poorest|underperforming)\b", t))
    which = bool(re.search(r"\b(which|who|what)\b", t))
    plan.limit = _limit(t)
    plan.order = "asc" if asc else "desc"
    plan.show_ids = bool(re.search(r"\b(id|ids|code|codes)\b", t))
    plan.growth = bool(re.search(r"\bgrowth\b|\bgrew\b|\bincrease\b|\bdecline\b|\bchange\b", t))

    breakdown_dims = [d for d in dims_found if d in ALL_DIMS]
    if grain == "day" and re.search(r"which (day|date)", t):
        breakdown_dims = ["day"]
    if compare_periods:
        plan.shape = "compare_periods"
        plan.compare_periods = compare_periods
        plan.dims = breakdown_dims[:1]
    elif plan.members:
        plan.shape = "compare_members"
        hint = plan.members[0].dim_hint
        plan.dims = [hint] if hint else []
    elif trendy or (grain and not breakdown_dims and not extreme_word):
        plan.shape = "trend"
        plan.dims = []
        if breakdown_dims:
            plan.notes.append("Trends are shown for the total; ask \"by zone\" separately for a split.")
    elif breakdown_dims and (extreme_word or which):
        plan.shape = "ranking"
        plan.dims = breakdown_dims[:1]
        plan.extreme = bool(which) or plan.limit is None and bool(re.search(r"\b(lowest|highest|least|maximum|minimum|max|min|most|best|worst)\b", t)) and not re.search(r"\b(skus|brands|reps|outlets|retailers|distributors|territories|cities|states)\b", t)
    elif breakdown_dims:
        plan.shape = "breakdown"
        plan.dims = breakdown_dims[:2]
        if re.search(r"\branking\b|\brank\b", t):
            plan.shape = "ranking"
            plan.dims = breakdown_dims[:1]
    elif grain == "day" and extreme_word:
        plan.shape = "ranking"
        plan.dims = ["day"]
        plan.extreme = True
    else:
        plan.shape = "value"

    if plan.shape == "ranking" and plan.limit is None:
        plan.limit = 1 if plan.extreme and plan.dims and plan.dims[0] in ("rep", "outlet", "distributor", "sku") and re.search(r"\b(the|a)\s+(rep|outlet|distributor|sku|retailer)\b|\bwith the\b", t) else None

    # Growth questions compare against the previous period
    if plan.growth and plan.shape == "value" and plan.metrics:
        plan.shape = "compare_periods"

    # Grain for trends
    if plan.shape == "trend":
        plan.grain = grain if grain in ("day", "week", "month") else None

    # Default and explicit periods
    if compare_periods:
        plan.period = compare_periods[0]
    elif period is not None:
        plan.period = period
    elif plan.shape == "trend":
        plan.period = preset("last_n_months", today, fy_start_month, 6)
        plan.notes.append("No period given, so the trend covers the last 6 months.")
    else:
        plan.period = preset("mtd", today, fy_start_month)
        snapshot_only = plan.metrics and all(m in ("inactive_outlets",) for m in plan.metrics)
        if not snapshot_only and not (plan.metrics == ["active_outlets"]):
            plan.notes.append("No period given, so I used month to date (MTD).")

    # A single day has nothing to trend over: show the day's value
    if plan.shape == "trend" and plan.period and plan.period.days == 1:
        plan.shape = "value"
        plan.grain = None

    # Values over several months -> show by month (the total is in the summary)
    if plan.shape == "value" and plan.period and plan.period.days >= 60 and plan.metrics and \
            not any(m in ("active_outlets", "inactive_outlets") for m in plan.metrics):
        plan.shape = "trend"
        plan.grain = "month"

    if not plan.metrics:
        plan.confidence = 0.2 if not breakdown_dims else 0.4
    return plan


def needs_llm(plan: Plan) -> bool:
    return plan.intent == "data" and not plan.metrics


def apply_llm_plan(plan: Plan, data: dict, today: date, fy_start_month: int = 4) -> Plan:
    """Merge a validated LLM plan (JSON) into the rule plan."""
    metrics = [m for m in data.get("metrics", []) if m in METRICS][:3]
    if not metrics:
        return plan
    plan.metrics = metrics
    plan.source = "llm"
    shape = data.get("shape")
    if shape in ("value", "trend", "breakdown", "ranking"):
        plan.shape = shape
    dims = [d for d in data.get("dims", []) if d in ALL_DIMS or d in ("day", "week", "month")][:2]
    if dims:
        plan.dims = dims
    if data.get("order") in ("asc", "desc"):
        plan.order = data["order"]
    if isinstance(data.get("limit"), int) and 0 < data["limit"] <= 100:
        plan.limit = data["limit"]
    if not plan.period_explicit and data.get("period") in (
            "today", "yesterday", "this_week", "last_week", "mtd", "last_month", "qtd", "last_quarter", "ytd"):
        plan.period = preset(data["period"], today, fy_start_month)
    elif not plan.period_explicit and data.get("start_date"):
        # A custom range the model read from the question, e.g. "the Diwali week"
        try:
            s_ = date.fromisoformat(str(data["start_date"])[:10])
            e_ = date.fromisoformat(str(data.get("end_date") or data["start_date"])[:10])
            if s_ <= e_ <= today and (e_ - s_).days < 800:
                from .timeparse import _make
                plan.period = _make(s_, e_, today)
        except ValueError:
            pass
    for f in data.get("filters", [])[:3]:
        if isinstance(f, dict) and f.get("value"):
            dim = f.get("dim") if f.get("dim") in ALL_DIMS else None
            plan.filters.append(RawFilter(str(f["value"]).lower()[:40], dim))
    plan.confidence = 0.7
    return plan


GEO_ORDER = list(GEO_DIMS)
