"""The answer pipeline: interpret (10s) -> query via Pulse MCP (20s) -> answer (20s), 55s hard stop.

`run` is an async generator of events the UI streams:
    {"type": "stage", "stage": "interpret" | "query" | "answer", "status": "start" | "done", ...}
    {"type": "answer", "answer": {...}}
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, AsyncIterator

from . import analyst, config, fmt, glossary, hindi, learn, prefs, rca, smalltalk
from .catalog import DIM_LABELS, GEO_DIMS, METRICS, SOURCES, TIME_GRAINS, pick_variant
from .entities import _match, resolve
from .guard import REFUSALS, GuardViolation, check_rows, check_sql, gate_question, other_brand_in
from .interpreter import Plan, RawFilter, apply_llm_plan, needs_llm, parse
from .llm import LocalLLM, numbers_in
from .mcp_client import DataError, PulseMCP
from .sqlbuild import LISTABLE, Filter, build_list, build_lookup_search, build_member_list, build_metric
from .timeparse import Period, grain_for, previous_period

# --------------------------------------------------------------------------- context


@dataclass
class CompanyCtx:
    id: int
    name: str
    tz_offset: int = 330
    fy_start_month: int = 4

    @property
    def today(self) -> date:
        from .timeparse import company_today
        return company_today(self.tz_offset)


_CACHE: dict[str, tuple[float, list[dict]]] = {}


async def _query(session, sql: str, company_id: int, timeout: float) -> list[dict]:
    check_sql(sql, company_id)  # Layer 3: never send unchecked SQL
    hit = _CACHE.get(sql)
    if hit and time.monotonic() - hit[0] < config.CACHE_TTL_SECONDS:
        return hit[1]
    result = await session.ch_query(sql, top=config.MAX_ROWS, timeout=timeout)
    check_rows(result.rows, company_id)  # Layer 4
    _CACHE[sql] = (time.monotonic(), result.rows)
    return result.rows


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- jobs


@dataclass
class Job:
    tag: str
    metric_id: str
    keys: list[str]
    period: Period | None
    filters: list[Filter]
    order: str | None = None
    limit: int | None = None
    key_in: Filter | None = None
    min_den: int | None = None


def _key_label(row: dict, i: int, key: str) -> str:
    name = row.get(f"k{i}_name")
    raw = row.get(f"k{i}")
    try:
        if key == "month" and raw is not None:
            s = str(raw)
            return date(int(s[:4]), int(s[4:6]), 1).strftime("%b %Y")
        if key in ("day", "week") and raw:
            d = date.fromisoformat(str(raw)[:10])
            return d.strftime("%d %b").lstrip("0") if key == "day" else "Week of " + d.strftime("%d %b").lstrip("0")
    except ValueError:
        return str(raw)
    if name not in (None, ""):
        return str(name).strip()
    if raw in (None, "", 0):
        return "Not mapped"
    return str(raw)


# --------------------------------------------------------------------------- answers


def _simple_answer(kind: str, text: str, **extra) -> dict:
    return {"kind": kind, "summary": text, "chart": None, "table": None, "formula_notes": [], "notes": [],
            "terms": [], "suggestions": extra.pop("suggestions", []), **extra}


HELP_SUGGESTIONS = [
    "Show primary sales for MTD", "Show TC vs PC trend", "Which zone has lowest coverage?",
    "Top 10 brands by secondary sales", "Show UBO for last month", "What is UBO?",
]


def _glossary_answer(plan: Plan, ctx: CompanyCtx) -> dict:
    saved = prefs.load(ctx.id)
    blocks = []
    notes = []
    for name in plan.glossary_terms[:3]:
        t = glossary.lookup(name)
        if not t:
            continue
        text = f"**{t.term}** stands for **{t.full_form}**. {t.definition}"
        if t.metric_id and t.metric_id in METRICS:
            m = METRICS[t.metric_id]
            v = m.variant(saved.get(m.id))
            text += f" Basic formula: {m.default.formula}."
            if m.id in saved:
                text += f" For {ctx.name} I use your chosen formula: {v.formula}."
            if m.ambiguous:
                notes.append(_formula_note(m.id, v.id, ctx, m.id in saved))
        elif t.basic_formula:
            text += f" Basic formula: {t.basic_formula}."
        blocks.append(text)
    if not blocks:
        return _simple_answer("clarify", "I don't have that term in my FMCG glossary yet.")
    ans = _simple_answer("glossary", "\n\n".join(blocks))
    ans["formula_notes"] = notes
    return ans


def _formula_note(metric_id: str, used_id: str, ctx: CompanyCtx, saved: bool) -> dict:
    m = METRICS[metric_id]
    used = m.variant(used_id)
    term = glossary.by_metric(metric_id)
    if term and term.full_form.lower().startswith(term.term.lower()):
        name = term.full_form
    elif term:
        name = f"{term.term} ({term.full_form})"
    else:
        name = m.label
    basic = m.default
    if saved:
        message = f"Using {ctx.name}'s chosen formula for {name}: {used.formula}."
    elif used.id == basic.id:
        message = (f"I used the basic formula for {name}: {used.formula}. If {ctx.name} follows a different formula, "
                   f"please choose it below and I'll use it for all future answers.")
    else:
        message = (f"The basic formula for {name} is: {basic.formula}. For this answer I used \"{used.label}\" "
                   f"({used.formula}), because the basic one doesn't fit this question. If {ctx.name} follows a "
                   f"different formula, please choose it below and I'll use it for all future answers.")
    return {
        "metric": metric_id, "metric_label": m.label, "saved": saved, "message": message,
        "used": {"id": used.id, "label": used.label, "formula": used.formula},
        "options": [{"id": v.id, "label": v.label, "formula": v.formula} for v in m.variants[:4]],
    }


def _choose_chart(plan: Plan, metric_ids: list[str], rows: list[dict], keys: list[str], question: str) -> str:
    units = {METRICS[m].unit for m in metric_ids}
    n = len(rows)
    if plan.shape == "value" or not keys:
        if len(metric_ids) == 1:
            return "kpi"
        return "bar" if len(units) == 1 else "kpi"
    if plan.shape == "trend" or (keys and keys[0] in TIME_GRAINS):
        return "bar" if n <= 40 else "table"
    if len(keys) > 1 or n > 15 or plan.show_ids:
        return "table"
    if plan.shape in ("compare_periods", "compare_members"):
        return "bar" if len(units) == 1 else "table"
    additive = all(METRICS[m].unit in ("currency", "count") and not METRICS[m].id.endswith("_pct") for m in metric_ids)
    share_words = re.search(r"\b(share|split|mix|contribution|distribution|break ?up|composition)\b", question.lower())
    if (len(metric_ids) == 1 and additive and 2 <= n <= 6 and plan.shape == "breakdown"
            and not re.search(r"\bcompare\b|\bacross\b", question.lower())):
        return "pie"
    if share_words and len(metric_ids) == 1 and additive and n <= 8:
        return "pie"
    return "hbar" if len(metric_ids) == 1 else "bar"


def _facts_and_summary(plan: Plan, ctx: CompanyCtx, metric_ids: list[str], rows: list[dict], keys: list[str],
                       period_label: str, filters_label: str, highlight: str | None) -> tuple[str, str, str | None]:
    """Template summary (always correct) plus facts text for the LLM. Returns (facts, summary, headline)."""
    where = f" for {filters_label}" if filters_label else ""
    lines: list[str] = []
    headline = None
    if not keys:
        parts = []
        for m in metric_ids:
            row = rows[0] if rows else {}
            val = row.get(m)
            parts.append(f"{METRICS[m].label} is {fmt.value(val, METRICS[m].unit)}")
            lines.append(f"{METRICS[m].label}: {fmt.value(val, METRICS[m].unit)}")
        if len(metric_ids) == 1 and rows:
            headline = fmt.value(rows[0].get(metric_ids[0]), METRICS[metric_ids[0]].unit)
        if len(metric_ids) == 2 and all(METRICS[m].unit == "currency" for m in metric_ids) and rows:
            a, b = rows[0].get(metric_ids[0]), rows[0].get(metric_ids[1])
            if a is not None and b is not None and "invoice_value" in metric_ids[0]:
                parts.append(f"together {fmt.money(a + b)}")
                lines.append(f"Total: {fmt.money(a + b)}")
        if set(metric_ids) == {"tc", "pc"} and rows:
            tc, pc = rows[0].get("tc"), rows[0].get("pc")
            if tc:
                parts.append(f"so {fmt.percent(pc / tc * 100)} of calls were productive")
                lines.append(f"Productive call %: {fmt.percent(pc / tc * 100)}")
        summary = f"{period_label}{where}: " + ", ".join(parts) + "."
    else:
        m0 = metric_ids[0]
        unit = METRICS[m0].unit
        label = DIM_LABELS.get(keys[0], keys[0])
        valid = [r for r in rows if r.get(m0) is not None]
        for r in rows[:25]:
            vals = ", ".join(f"{METRICS[m].label} {fmt.value(r.get(m), METRICS[m].unit)}" for m in metric_ids)
            lines.append(f"{r['label']}: {vals}")
        if plan.shape in ("trend",) or keys[0] in TIME_GRAINS and plan.shape != "ranking":
            if valid:
                first, last = valid[0], valid[-1]
                best = max(valid, key=lambda r: r[m0])
                total_txt = ""
                if unit in ("currency", "count") and m0 not in ("ubo", "visited_outlets", "productive_outlets", "active_outlets"):
                    total = sum(r[m0] for r in valid)
                    total_txt = f" The total for the period is {fmt.value(total, unit)}."
                    lines.append(f"Total: {fmt.value(total, unit)}")
                    headline = fmt.value(total, unit)
                summary = (f"{METRICS[m0].label}{where}, {period_label}: it moved from {fmt.value(first[m0], unit)} "
                           f"({first['label']}) to {fmt.value(last[m0], unit)} ({last['label']}). The highest was "
                           f"{fmt.value(best[m0], unit)} in {best['label']}.{total_txt}")
                if len(metric_ids) > 1:
                    m1 = metric_ids[1]
                    tot0 = sum(r.get(m0) or 0 for r in rows)
                    tot1 = sum(r.get(m1) or 0 for r in rows)
                    if METRICS[m1].unit == unit and unit in ("currency", "count"):
                        summary += f" Over the period, {METRICS[m0].label} totals {fmt.value(tot0, unit)} and {METRICS[m1].label} {fmt.value(tot1, unit)}."
                        lines.append(f"{METRICS[m0].label} total: {fmt.value(tot0, unit)}; {METRICS[m1].label} total: {fmt.value(tot1, unit)}")
                        if {m0, m1} == {"tc", "pc"} and (tot0 or tot1):
                            tc_t = tot0 if m0 == "tc" else tot1
                            pc_t = tot1 if m0 == "tc" else tot0
                            if tc_t:
                                summary += f" Overall {fmt.percent(pc_t / tc_t * 100)} of calls were productive."
                                lines.append(f"Productive call %: {fmt.percent(pc_t / tc_t * 100)}")
            else:
                summary = f"I found no {METRICS[m0].label.lower()}{where} in {period_label}."
        elif plan.shape in ("compare_periods", "compare_members"):
            if len(valid) >= 2:
                a, b = valid[0], valid[1]
                ch = fmt.change(a[m0], b[m0])
                summary = (f"{METRICS[m0].label}{where}: {a['label']} {fmt.value(a[m0], unit)} vs "
                           f"{b['label']} {fmt.value(b[m0], unit)}" + (f", {ch}." if ch else "."))
                if ch:
                    lines.append(f"Change: {ch}")
                headline = fmt.value(a[m0], unit)
            elif valid:
                summary = f"{METRICS[m0].label}{where}: {valid[0]['label']} {fmt.value(valid[0][m0], unit)}; no data for the other."
            else:
                summary = f"I found no {METRICS[m0].label.lower()}{where} for these."
        else:
            if valid:
                top = valid[0]
                headline = top["label"]
                direction = "lowest" if plan.order == "asc" and plan.shape == "ranking" else "highest"
                extra = ""
                if plan.show_ids and top.get("id") is not None:
                    extra = f" (ID {top['id']})"
                summary = (f"{top['label']}{extra} has the {direction} {METRICS[m0].label.lower()}{where} in "
                           f"{period_label}, at {fmt.value(top[m0], unit)}.")
                if len(metric_ids) > 1:
                    summary += " " + ", ".join(f"{METRICS[m].label}: {fmt.value(top.get(m), METRICS[m].unit)}" for m in metric_ids[1:]) + "."
                if len(valid) > 1:
                    n = len(valid)
                    summary += f" Showing {n} {plural(label.lower(), n)}"
                    if unit in ("currency", "count") and plan.shape == "breakdown" and len(keys) == 1:
                        total = sum(r[m0] for r in valid)
                        share = top[m0] / total * 100 if total else 0
                        summary += f"; {top['label']} is {share:.1f}% of the total {fmt.value(total, unit)}."
                        lines.append(f"Total: {fmt.value(total, unit)}; top share {share:.1f}%")
                    else:
                        summary += "."
            else:
                summary = f"I found no {METRICS[m0].label.lower()}{where} in {period_label}."
    facts = f"Company: {ctx.name}\nPeriod: {period_label}\n" + (f"Filters: {filters_label}\n" if filters_label else "") + "\n".join(lines)
    return facts, summary, headline


# --------------------------------------------------------------------------- main


# --------------------------------------------------------------------------- master data: lists, lookups, unknown names

_FMCG = re.compile(
    r"\b(sales?|sell\w*|sold|revenue|orders?|invoices?|billing|billed|outlets?|retailers?|stores?|shops?|distributors?|"
    r"stockists?|dbs?|beats?|reps?|salesm[ae]n|dsrs?|skus?|products?|brands?|categor(y|ies)|zones?|regions?|"
    r"territor(y|ies)|states?|cit(y|ies)|schemes?|targets?|achievement|stock|coverage|penetration|calls?|visits?|"
    r"ubo|tc|pc|lpc|fmcg|market|mtd|ytd|qtd|kpi|growth|volume|value|cases?|units?|portfolio)\b", re.I)

LIST_CHILD_LABELS = {"zone": "Regions", "region": "Territories", "brand": "Active SKUs", "category": "Active SKUs",
                     "state": "Outlets", "city": "Outlets"}


def _fmcg_words(question: str) -> bool:
    return bool(_FMCG.search(question) or glossary.find_terms(question))


def _strip_company(question: str, name: str) -> str:
    """'product categories for Deoleo' -> 'product categories': the locked company is never a filter."""
    out = question
    for n in {name.strip(), name.strip().split()[0] if name.strip() else ""}:
        if len(n) >= 3:
            out = re.sub(rf"\b(?:for|of|in|at|from|by|under)?\s*(?:the\s+)?{re.escape(n)}(?:'s)?\b", " ", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip() or question


async def _member_names(session, ctx: CompanyCtx, dim: str, budget: float, n: int = 12) -> list[str]:
    rows = await _query(session, build_member_list(ctx.id, dim), ctx.id, timeout=budget)
    return list(dict.fromkeys(str(r["name"]).strip() for r in rows if str(r.get("name", "")).strip()))[:n]


async def _not_found_answer(session, ctx: CompanyCtx, missing: RawFilter, question: str, budget: float) -> dict:
    """A name the user typed doesn't exist for this company: say so plainly, never guess."""
    dim = missing.dim_hint or "zone"
    names = await _member_names(session, ctx, dim, budget)
    word = _dim_word(dim)
    if missing.dim_hint:
        lead = f"There is no {word} called \"{missing.text.title()}\" in {ctx.name}'s data"
    else:
        lead = (f"I couldn't find \"{missing.text.title()}\" in {ctx.name}'s zones, regions, territories, states, "
                f"cities, categories, brands or distributors")
    text = (f"{lead}, so I won't make up an answer. I only answer from {ctx.name}'s own data. "
            f"{plural(word, 2).capitalize() if not word.isupper() else word + 's'} you can ask about: {', '.join(names)}.")
    sugg = [re.sub(re.escape(missing.text), n, question, count=1, flags=re.I) for n in names[:4]]
    return _simple_answer("refusal", text, suggestions=[s for s in sugg if s != question])


def _join_names(names: list[str], limit: int = 15) -> str:
    shown = names[:limit]
    more = len(names) - len(shown)
    if more > 0:
        return ", ".join(shown) + f" and {more} more"
    if len(shown) > 1:
        return ", ".join(shown[:-1]) + " and " + shown[-1]
    return shown[0] if shown else ""


async def _list_answer(session, plan: Plan, ctx: CompanyCtx, question: str, budget: float) -> dict:
    dim = plan.list_dim
    res = await resolve(session, ctx.id, plan.filters, [])
    if res.unresolved:
        return await _not_found_answer(session, ctx, res.unresolved[0], question, budget)
    sql, ignored, extras = build_list(ctx.id, dim, res.filters, limit=LIST_LIMIT)
    raw = await _query(session, sql, ctx.id, timeout=budget)
    sqls = [sql]
    total = len(raw)
    if len(raw) >= LIST_LIMIT:  # long lists (outlets): count them properly
        count_sql = f"SELECT count() AS total FROM ({re.sub(r' ORDER BY .*$', '', sql)}) AS t"
        total = int(_num((await _query(session, count_sql, ctx.id, timeout=budget) or [{}])[0].get("total")) or len(raw))
        sqls.append(count_sql)
    rows = []
    for r in raw:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        row = {"label": name, "id": r.get("id")}
        if "n" in r:
            row["n"] = _num(r.get("n")) or 0
        for key, _ in extras:
            v = r.get(key)
            row[key] = _num(v) if key == "mrp" else (str(v).strip() if v not in (None, "") else "–")
        rows.append(row)

    applied = [f for f in res.filters if f not in ignored]
    where = (" in " + ", ".join(f"{_dim_word(f.dim)} {f.label}" for f in applied)) if applied else ""
    word = _dim_word(dim)
    many = plural(word, total) if not word.isupper() else (word + ("s" if total != 1 else ""))
    notes = [f"Counted from {ctx.name}'s master data: active, non-deleted records only."]
    if dim == "category":
        notes.append("A category that exists under several brands is shown once; the count is its active SKUs across brands.")
    for f in ignored:
        notes.append(f"A {_dim_word(dim)} list can't be filtered by {_dim_word(f.dim)}, so I didn't apply \"{f.label}\".")
    notes += res.notes

    if total > len(rows):
        notes.append(f"Showing the first {len(rows)} of {total} by name.")
    if not rows:
        summary = f"I found no active {plural(word, 2)}{where} in {ctx.name}'s master data."
        return {**_simple_answer("empty", summary), "notes": notes}
    names = [r["label"] for r in rows]
    if plan.count_only:
        summary = f"{ctx.name} has **{total:,}** active {many}{where}."
        if total <= 12:
            summary += f" They are: {_join_names(names)}."
    else:
        summary = f"{ctx.name} has {total:,} active {many}{where}: {_join_names(names)}."
        if dim == "category" and all("n" in r for r in rows):
            top = max(rows, key=lambda r: r["n"])
            summary += f" {top['label']} has the most SKUs ({fmt.value(top['n'], 'count')})."

    child = LIST_CHILD_LABELS.get(dim) if any("n" in r for r in rows) else None
    columns = [{"key": "label", "label": DIM_LABELS.get(dim, dim.title())}]
    if child:
        columns.append({"key": "n", "label": child, "unit": "count"})
    columns += [{"key": k, "label": lbl, **({"unit": "currency"} if k == "mrp" else {})} for k, lbl in extras]

    if plan.count_only:
        chart = {"type": "kpi", "series": [{"id": "count", "label": f"Active {plural(word, 2)}", "unit": "count"}],
                 "data": [{"label": "", "count": total}], "highlight": None, "x_label": ""}
    elif child and 2 <= len(rows) <= 15:
        pie = dim in ("category", "brand") and len(rows) <= 6
        chart = {"type": "pie" if pie else "hbar", "series": [{"id": "n", "label": child, "unit": "count"}],
                 "data": rows, "highlight": None, "x_label": DIM_LABELS.get(dim, "")}
    else:
        chart = {"type": "table", "series": [], "data": rows, "highlight": None, "x_label": ""}

    first = names[0]
    follow = {
        "category": [f"Secondary sales by category for MTD", f"List SKUs in {first} category", "Top 5 SKUs by secondary sales"],
        "brand": ["Secondary sales by brand for last month", f"List categories under {first} brand", "List all SKUs"],
        "sku": ["Top 10 SKUs by secondary sales", "Secondary sales by category for MTD"],
        "distributor": ["Top 10 distributors by primary sales", "Distributors by zone"],
        "rep": ["Top 5 sales reps by secondary sales this month", "Bottom 5 reps by PC %"],
    }.get(dim, [f"Secondary sales by {word} for MTD", f"Coverage by {word} for last month"])
    return {
        "kind": "data", "summary": summary, "summary_by": "template", "headline": f"{total:,}",
        "chart": chart, "table": {"columns": columns, "rows": rows}, "formula_notes": [], "notes": notes,
        "terms": glossary.abbreviations_in(summary), "suggestions": follow[:3],
        "how": {"period": f"Current master data (as of {fmt_date(ctx.today)})",
                "filters": ", ".join(f"{_dim_word(f.dim)} {f.label}" for f in applied) or "None",
                "metrics": [{"label": f"{DIM_LABELS.get(dim, dim)} list", "formula": "Active, non-deleted records" +
                             (f"; {child.lower()} counted per {word}" if child else ""),
                             "source": f"{ctx.name}'s {word} master"}],
                "interpreted_by": "local LLM" if plan.source == "llm" else "rules", "queries": len(sqls), "sql": sqls},
    }


LIST_LIMIT = 500
LOOKUP_DIMS = list(GEO_DIMS) + ["category", "brand", "distributor", "sku", "rep"]


async def _lookup_answer(session, plan: Plan, ctx: CompanyCtx, budget: float) -> dict:
    """"what is Pure", "who is Ramesh": find the name in this company's masters, or refuse."""
    term = plan.lookup_term.strip().lower()
    sql = build_lookup_search(ctx.id, [term], LOOKUP_DIMS)
    rows = await _query(session, sql, ctx.id, timeout=budget)
    found: dict[str, list[str]] = {}
    for r in rows:
        name = str(r.get("name") or "").strip()
        if r.get("dim") in LOOKUP_DIMS and _match(term, name):
            found.setdefault(r["dim"], [])
            if name not in found[r["dim"]]:
                found[r["dim"]].append(name)
    if not found:
        return _simple_answer(
            "refusal",
            f"I don't know \"{plan.lookup_term.title()}\": it isn't in {ctx.name}'s data or in my FMCG glossary, and "
            f"answering general questions is against my policy. I only answer FMCG analytics questions about "
            f"{ctx.name}'s own sales, calls, coverage, outlets, distributors, products and sales reps.",
            suggestions=HELP_SUGGESTIONS[:3])
    parts = [f"a {_dim_word(d)}" + (f" ({', '.join(n[:3])})" if len(n) > 1 or n[0].lower() != term else "")
             for d, n in found.items()]
    title = next(iter(found.values()))[0]
    summary = f"**{title}** is {' and '.join(parts)} in {ctx.name}'s data."
    sugg = []
    for d in found:
        if d in ("category", "brand"):
            sugg += [f"Secondary sales for {title} {d} this month", f"List SKUs in {title} {d}"]
        elif d in GEO_DIMS:
            sugg += [f"Secondary sales for {title} {d} this month", f"Coverage in {title} {d} last month"]
        elif d == "rep":
            sugg += [f"Productive calls by sales rep this month"]
        elif d == "distributor":
            sugg += [f"Primary sales for {title} distributor last month"]
    return _simple_answer("glossary", summary, suggestions=sugg[:3])


async def run(question: str, ctx: CompanyCtx, *, llm: LocalLLM | None = None, mcp: PulseMCP | None = None,
              use_llm_summary: bool = True) -> AsyncIterator[dict]:
    started = time.monotonic()
    llm = llm or LocalLLM()
    mcp = mcp or PulseMCP()
    timings: dict[str, float] = {}
    raw = question
    lang = "en"
    greeting = ""  # a warm first line when the question came with a hello
    learned_info: dict | None = None  # a metric Cooper learned from the FieldAssist docs

    def left() -> float:
        return config.BUDGET_TOTAL - (time.monotonic() - started)

    def finish(answer: dict) -> dict:
        timings["total"] = round(time.monotonic() - started, 2)
        answer["timings"] = timings
        answer["company"] = {"id": ctx.id, "name": ctx.name}
        answer["question"] = raw
        answer["lang"] = lang
        if greeting and answer.get("kind") not in ("refusal", "error"):
            answer["summary"] = f"{greeting} {answer['summary']}"
        if hindi.is_hindi(lang) and answer.get("kind") != "help":
            line = hindi.summary_line(answer, lang, ctx.name)
            if line:
                answer["summary_hi"] = line
        if learned_info:
            answer["learned"] = learned_info
        if question.strip().lower() != raw.strip().lower():
            if answer.get("how"):
                answer["how"]["understood_as"] = question
            elif answer.get("kind") not in ("help", "refusal"):
                answer.setdefault("notes", []).append(f"Understood as: \"{question}\"")
        return {"type": "answer", "answer": answer}

    # ---------------- 1. interpret
    yield {"type": "stage", "stage": "interpret", "status": "start"}
    t = time.monotonic()
    # Greetings and small talk get a warm reply before any guardrail or data query.
    st = smalltalk.analyse(raw)
    if st.pure and not (gate_question(raw, ctx.id, company_name=ctx.name)
                        or gate_question(hindi.normalize(raw)[0], ctx.id, company_name=ctx.name)):
        lang = st.lang
        text, chips = smalltalk.reply(st, ctx.name, raw)
        yield {"type": "stage", "stage": "interpret", "status": "done", "detail": "Small talk"}
        yield finish(_simple_answer("help", text, suggestions=chips))
        return
    work = raw
    if st.found and smalltalk.has_question(st):
        work, greeting = st.rest, smalltalk.prefix(st)
    # Hindi / Hinglish / misheard speech -> the English the parser reads. The gate checks both texts.
    if "\n" in work.strip():
        # a multi-line ask: keep the lines apart so numbered parts stay numbered
        parts = [hindi.normalize(ln) for ln in work.splitlines() if ln.strip()]
        question = "\n".join(p[0] for p in parts)
        lang = next((p[1] for p in parts if p[1] != "en"), "en")
    else:
        question, lang = hindi.normalize(work)
    if st.found and hindi.is_hindi(st.lang):
        lang = st.lang if lang == "en" else lang
    reason = gate_question(raw, ctx.id, company_name=ctx.name) or gate_question(question, ctx.id, company_name=ctx.name)
    if reason:
        yield {"type": "stage", "stage": "interpret", "status": "done", "detail": "Blocked by guardrail"}
        name = other_brand_in(raw, ctx.name) or other_brand_in(question, ctx.name) or ""
        text = REFUSALS[reason].format(company=ctx.name, name=name.title())
        yield finish(_simple_answer("refusal", text, suggestions=HELP_SUGGESTIONS[:3]))
        return
    today = ctx.today
    # A metric this company taught Cooper before, or one it doesn't know yet: learn it from the FieldAssist docs
    forced_metric = None
    entry = learn.find_learned(question, ctx.id)
    unknown = None if entry else learn.find_unknown(raw if not hindi.is_hindi(lang) else question)
    if entry or unknown:
        if not entry:
            yield {"type": "stage", "stage": "interpret", "status": "done", "detail": f"Looking up \"{unknown}\" in FieldAssist docs"}
            try:
                async with mcp.session() as session:
                    entry = await asyncio.wait_for(learn.learn(session, unknown, ctx.id, ctx.name, llm), timeout=learn.DOC_BUDGET + 4)
            except (asyncio.TimeoutError, DataError) as exc:
                entry = None
                if isinstance(exc, DataError):
                    yield finish(_simple_answer("error", f"I couldn't reach Pulse to look up \"{unknown}\": {exc}"))
                    return
        if entry:
            learned_info = learn.describe(entry)
            if not entry.get("alternatives"):
                text = (f"**{entry['term']}** isn't something I can calculate from {ctx.name}'s Pulse data yet. "
                        + (f"What it means: {entry['definition']} " if entry.get("definition") else "")
                        + (f"({entry['source']}.) " if entry.get("source") else "")
                        + f"Calculating it needs {entry.get('missing') or 'data that Pulse does not hold'} for {ctx.name}, "
                        "and I won't guess a number.")
                ans = _simple_answer("glossary", text, suggestions=HELP_SUGGESTIONS[:3])
                ans["notes"] = list(entry.get("flags", []))
                yield finish(ans)
                return
            forced_metric = learn.register(ctx.id, entry)
    stripped = "\n".join(_strip_company(ln, ctx.name) for ln in question.splitlines() if ln.strip()) or question
    if not forced_metric and analyst.is_analysis(stripped):
        aplan = analyst.parse_analysis(stripped, today, ctx.fy_start_month)
        if aplan.why and not aplan.sections and aplan.refs and rca.FAMILY.get(analyst.why_metric(aplan)) and                 not any(r.a in ("inactive_outlets", "active_outlets", "new_outlets") for r in aplan.refs[:1]):
            # "why did primary fall in North, and which 5 distributors caused most of it": the root cause engine, scoped
            mid = analyst.why_metric(aplan)
            rplan = Plan(intent="rca", metrics=[mid], period=aplan.period, filters=aplan.filters, notes=list(aplan.notes))
            timings["interpret"] = round(time.monotonic() - t, 2)
            async for ev in _run_rca(rplan, ctx, question, llm, mcp, timings, left, use_llm_summary,
                                     contrib_dims=aplan.contrib_dims or ([aplan.entity] if aplan.entity else None),
                                     contrib_n=aplan.contrib_n):
                yield finish(ev) if ev.get("kind") else ev
            return
        timings["interpret"] = round(time.monotonic() - t, 2)
        async for ev in _run_analyst(aplan, ctx, question, mcp, timings, left):
            yield finish(ev) if ev.get("kind") else ev
        return
    plan = parse(_strip_company(question, ctx.name), today, ctx.fy_start_month)
    if forced_metric:
        plan.intent = "data"
        plan.metrics = [forced_metric]
        if plan.shape not in ("value", "trend", "breakdown", "ranking", "compare_periods"):
            plan.shape = "value"
        variant = METRICS[forced_metric].default
        dropped = [d for d in plan.dims if d not in TIME_GRAINS and not variant.supports(d)]
        if dropped:
            # "must-sell SKU %": "SKU" is part of the metric's name, and the formula can't be split by it anyway
            plan.dims = [d for d in plan.dims if d not in dropped]
            if not plan.dims and plan.shape in ("breakdown", "ranking"):
                plan.shape = "value"
            plan.notes.append(f"{learned_info['term']} can't be split by "
                              f"{', '.join(DIM_LABELS.get(d, d).lower() for d in dropped)} with the data Pulse has, so this is the total.")
        plan.notes.append(f"{learned_info['term']} is a learned metric: {learned_info['label']}.")
    if plan.intent == "data" and needs_llm(plan):
        data = await llm.interpret(question, timeout=max(2.0, config.BUDGET_INTERPRET - (time.monotonic() - t)))
        if data:
            plan = apply_llm_plan(plan, data, today, ctx.fy_start_month)
            if not plan.metrics and data.get("list_of") in LISTABLE:
                plan.intent, plan.list_dim, plan.source = "list", data["list_of"], "llm"
    timings["interpret"] = round(time.monotonic() - t, 2)

    if plan.intent == "help":
        yield {"type": "stage", "stage": "interpret", "status": "done", "detail": "Greeting"}
        yield finish(_simple_answer("help", f"Hi! I'm Cooper. Ask me about {ctx.name}'s sales, calls, coverage, "
                                            "penetration, UBO, outlets or sales reps, for any period.",
                                    suggestions=HELP_SUGGESTIONS))
        return
    if plan.intent == "glossary":
        yield {"type": "stage", "stage": "interpret", "status": "done", "detail": "Glossary question"}
        yield finish(_glossary_answer(plan, ctx))
        return
    if plan.intent in ("list", "lookup"):
        what = f"List of {plural(_dim_word(plan.list_dim), 2)}" if plan.intent == "list" else f"Looking up \"{plan.lookup_term}\""
        yield {"type": "stage", "stage": "interpret", "status": "done", "detail": what, "plan": plan.to_dict()}
        yield {"type": "stage", "stage": "query", "status": "start"}
        t = time.monotonic()
        budget = min(config.BUDGET_QUERY, max(5.0, left() - 5))
        try:
            async with mcp.session() as session:
                if plan.intent == "list":
                    answer = await asyncio.wait_for(_list_answer(session, plan, ctx, question, budget), timeout=budget)
                else:
                    answer = await asyncio.wait_for(_lookup_answer(session, plan, ctx, budget), timeout=budget)
        except asyncio.TimeoutError:
            answer = _simple_answer("error", "The data took longer than my time limit to come back. Please try again.")
        except GuardViolation as exc:
            answer = _simple_answer("refusal", f"I stopped this answer because a safety check failed ({exc}). No data was shown.")
        except DataError as exc:
            answer = _simple_answer("error", f"I couldn't get the data from Pulse just now: {exc}")
        timings["query"] = round(time.monotonic() - t, 2)
        yield {"type": "stage", "stage": "query", "status": "done", "detail": f"Master data · {timings['query']}s"}
        yield {"type": "stage", "stage": "answer", "status": "start"}
        yield {"type": "stage", "stage": "answer", "status": "done"}
        yield finish(answer)
        return
    if plan.intent == "rca":
        async for ev in _run_rca(plan, ctx, question, llm, mcp, timings, left, use_llm_summary):
            yield finish(ev) if ev.get("kind") else ev
        return
    if not plan.metrics:
        yield {"type": "stage", "stage": "interpret", "status": "done", "detail": "Not understood"}
        if not _fmcg_words(question):
            # Neither the rules nor the local model found anything FMCG in it: out of scope, never a guess.
            yield finish(_simple_answer("refusal", REFUSALS["off_topic"].format(company=ctx.name),
                                        suggestions=HELP_SUGGESTIONS[:3]))
            return
        yield finish(_simple_answer(
            "clarify",
            f"I couldn't match that to {ctx.name}'s data, and I won't guess. I can answer questions on primary and "
            "secondary sales, invoice value, orders, TC, PC, PC %, coverage, penetration, UBO, outlets, distributors, "
            "products (categories, brands, SKUs) and sales rep performance, for any date range, zone or product.",
            suggestions=HELP_SUGGESTIONS))
        return

    # Plan fixes that need catalog knowledge
    if any(m.startswith("primary") for m in plan.metrics) and "outlet" in plan.dims:
        plan.dims = ["distributor" if d == "outlet" else d for d in plan.dims]
        plan.notes.append("Primary sales are billed to distributors, not retailers, so I ranked distributors instead.")
    saved = prefs.load(ctx.id)
    if "active_outlets" in plan.metrics and plan.period_explicit and "active_outlets" not in saved:
        saved = {**saved, "active_outlets": "on_beats"}
        plan.notes.append("For a specific period, active outlets are counted as outlets on salesmen's mapped beats in that period.")

    detail = ", ".join(METRICS[m].label for m in plan.metrics) + f" · {plan.period.label if plan.period else ''}"
    yield {"type": "stage", "stage": "interpret", "status": "done", "detail": detail, "plan": plan.to_dict()}

    # ---------------- 2. query via Pulse MCP
    yield {"type": "stage", "stage": "query", "status": "start"}
    t = time.monotonic()
    budget = min(config.BUDGET_QUERY, max(5.0, left() - 10))
    try:
        async with mcp.session() as session:
            # Placeholders such as "xyz zone": ask which one, with real names to pick from
            if plan.placeholder_dim:
                dim = plan.placeholder_dim
                rows = await _query(session, build_member_list(ctx.id, dim), ctx.id, timeout=budget)
                names = [str(r["name"]).strip() for r in rows if str(r.get("name", "")).strip()][:12]
                timings["query"] = round(time.monotonic() - t, 2)
                yield {"type": "stage", "stage": "query", "status": "done", "detail": "Need one more detail"}
                sugg = [re.sub(r"\b(xyz|abc|xxx)\b(\s*[a-z]+(\s*/\s*[a-z]+)*)?", f"{n} {dim}", question, count=1, flags=re.I)
                        for n in names[:6]]
                yield finish(_simple_answer(
                    "clarify", f"Which {DIM_LABELS[dim].lower()} do you mean? {ctx.name} has: " + ", ".join(names) + ".",
                    suggestions=sugg))
                return

            res = await resolve(session, ctx.id, plan.filters, plan.members)
            if res.unresolved:
                answer = await _not_found_answer(session, ctx, res.unresolved[0], question, budget)
                timings["query"] = round(time.monotonic() - t, 2)
                yield {"type": "stage", "stage": "query", "status": "done", "detail": "Name not found"}
                yield finish(answer)
                return
            plan.notes += res.notes

            jobs = _plan_jobs(plan, res.filters, res.members, today, ctx)
            outcome = await asyncio.wait_for(_run_jobs(session, jobs, plan, ctx, saved), timeout=budget)
    except asyncio.TimeoutError:
        timings["query"] = round(time.monotonic() - t, 2)
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Timed out"}
        yield finish(_simple_answer("error", "The data took longer than my time limit to come back. Try a shorter "
                                             "period or a narrower question, and I'll answer within a minute."))
        return
    except GuardViolation as exc:
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Blocked by guardrail"}
        yield finish(_simple_answer("refusal", f"I stopped this answer because a safety check failed ({exc}). "
                                               f"No data was shown."))
        return
    except DataError as exc:
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Data source error"}
        yield finish(_simple_answer("error", f"I couldn't get the data from Pulse just now: {exc}"))
        return
    timings["query"] = round(time.monotonic() - t, 2)
    yield {"type": "stage", "stage": "query", "status": "done",
           "detail": f"{outcome['queries']} quer{'y' if outcome['queries'] == 1 else 'ies'} · {timings['query']}s"}

    # ---------------- 3. answer
    yield {"type": "stage", "stage": "answer", "status": "start"}
    t = time.monotonic()
    answer = _compose(plan, ctx, outcome, question)
    if use_llm_summary and config.RCA_LLM_REWORD and llm.enabled and left() > 8 and answer.get("facts"):
        allowed = numbers_in(answer["facts"]) | numbers_in(answer["summary"])
        text = await llm.summarise(question, answer["facts"], allowed,
                                   timeout=min(config.BUDGET_ANSWER, max(3.0, left() - 5)))
        if text:
            answer["summary"] = text
            answer["summary_by"] = "llm"
    answer.pop("facts", None)
    timings["answer"] = round(time.monotonic() - t, 2)
    yield {"type": "stage", "stage": "answer", "status": "done"}
    yield finish(answer)


async def _run_analyst(aplan, ctx: CompanyCtx, question: str, mcp: PulseMCP, timings: dict, left) -> AsyncIterator[dict]:
    """Multi-constraint questions: several metrics, a ranking, two periods, filters and conditions."""
    parts = aplan.sections or [aplan]
    detail = "; ".join(filter(None, [
        ", ".join(dict.fromkeys(r.name() for p in parts for r in p.refs))[:80],
        f"{'top' if aplan.top_dir == 'desc' else 'bottom'} {aplan.top_n}" if aplan.top_n else "",
        f"{len(aplan.conds)} condition{'s' if len(aplan.conds) != 1 else ''}" if aplan.conds else "",
        f"{len(aplan.sections)} parts" if aplan.sections else ""]))
    yield {"type": "stage", "stage": "interpret", "status": "done", "detail": f"Analysis: {detail}"}
    yield {"type": "stage", "stage": "query", "status": "start"}
    t = time.monotonic()
    budget = min(config.BUDGET_RCA_QUERY, max(5.0, left() - 6))
    saved = prefs.load(ctx.id)
    try:
        async with mcp.session() as session:
            async def resolver(raws):
                return await resolve(session, ctx.id, raws, [])
            try:
                answer = await asyncio.wait_for(analyst.run_analysis(session, _query, aplan, ctx, saved, budget, resolver),
                                                timeout=budget)
            except analyst.AnalystError as exc:
                missing = exc.args[0] if exc.args else None
                if isinstance(missing, RawFilter):
                    answer = await _not_found_answer(session, ctx, missing, question, budget)
                else:
                    reasons = exc.args[1] if len(exc.args) > 1 else []
                    answer = _simple_answer("clarify", "I couldn't measure what you asked for with the data Pulse has for "
                                                       f"{ctx.name}. " + " ".join(reasons[:2]),
                                            suggestions=HELP_SUGGESTIONS[:3])
    except asyncio.TimeoutError:
        timings["query"] = round(time.monotonic() - t, 2)
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Timed out"}
        yield _simple_answer("error", "That analysis took longer than my time limit. Try a shorter period, fewer metrics or one "
                                      "zone, and I'll answer within a minute.")
        return
    except GuardViolation as exc:
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Blocked by guardrail"}
        yield _simple_answer("refusal", f"I stopped this answer because a safety check failed ({exc}). No data was shown.")
        return
    except DataError as exc:
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Data source error"}
        yield _simple_answer("error", f"I couldn't get the data from Pulse just now: {exc}")
        return
    timings["query"] = round(time.monotonic() - t, 2)
    answer.pop("_top", None)
    n = (answer.get("how") or {}).get("queries", 0)
    yield {"type": "stage", "stage": "query", "status": "done", "detail": f"{n} queries · {timings['query']}s"}
    yield {"type": "stage", "stage": "answer", "status": "start"}
    if answer.get("kind") in ("data", "empty"):
        ambiguous = list(dict.fromkeys(mid for p in parts for r in p.refs for mid, _ in r.bases()
                                       if mid in METRICS and METRICS[mid].ambiguous))
        answer["formula_notes"] = [_formula_note(m, saved.get(m) or METRICS[m].default.id, ctx, m in saved) for m in ambiguous[:3]]
        answer["terms"] = glossary.abbreviations_in(answer["summary"])
        answer.setdefault("suggestions", [])
    timings["answer"] = 0.0
    yield {"type": "stage", "stage": "answer", "status": "done"}
    yield answer


async def _run_rca(plan: Plan, ctx: CompanyCtx, question: str, llm: LocalLLM, mcp: PulseMCP, timings: dict,
                   left, use_llm_summary: bool, contrib_dims: list[str] | None = None,
                   contrib_n: int = 5) -> AsyncIterator[dict]:
    """Root cause analysis. Yields stage events, then the answer dict (wrapped by the caller)."""
    metric_id = plan.metrics[0]
    saved = prefs.load(ctx.id)
    if metric_id == "inactive_outlets":
        yield {"type": "stage", "stage": "interpret", "status": "done", "detail": "Snapshot metric"}
        yield _simple_answer("clarify", f"Inactive outlets is a count of today's blocked outlets, so I can't compare it "
                                        "over time. Ask why active outlets, coverage or UBO changed instead.",
                             suggestions=["Why did coverage drop this month?", "Why is UBO low this month?"])
        return
    if metric_id == "active_outlets":
        saved = {**saved, "active_outlets": "on_beats"}
        plan.notes.append("To compare periods, active outlets are counted as outlets on salesmen's mapped beats.")
    yield {"type": "stage", "stage": "interpret", "status": "done", "plan": plan.to_dict(),
           "detail": f"Why: {METRICS[metric_id].label} · {plan.period.label}"}
    yield {"type": "stage", "stage": "query", "status": "start"}
    t = time.monotonic()
    budget = min(config.BUDGET_RCA_QUERY, max(5.0, left() - 8))
    try:
        async with mcp.session() as session:
            res = await resolve(session, ctx.id, plan.filters, [])
            if res.unresolved:
                answer = await _not_found_answer(session, ctx, res.unresolved[0], question, budget)
                timings["query"] = round(time.monotonic() - t, 2)
                yield {"type": "stage", "stage": "query", "status": "done", "detail": "Name not found"}
                yield answer
                return
            answer = await asyncio.wait_for(
                rca.analyse(session, _query, ctx=ctx, metric_id=metric_id, period=plan.period, filters=res.filters,
                            saved=saved, question=question, budget=budget, extra_notes=plan.notes + res.notes,
                            contrib_dims=contrib_dims, contrib_n=contrib_n),
                timeout=budget)
    except asyncio.TimeoutError:
        timings["query"] = round(time.monotonic() - t, 2)
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Timed out"}
        yield _simple_answer("error", "The analysis took longer than my time limit. Try a shorter period or one zone, "
                                      "and I'll answer within a minute.")
        return
    except GuardViolation as exc:
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Blocked by guardrail"}
        yield _simple_answer("refusal", f"I stopped this answer because a safety check failed ({exc}). No data was shown.")
        return
    except DataError as exc:
        yield {"type": "stage", "stage": "query", "status": "done", "detail": "Data source error"}
        yield _simple_answer("error", f"I couldn't get the data from Pulse just now: {exc}")
        return
    timings["query"] = round(time.monotonic() - t, 2)
    yield {"type": "stage", "stage": "query", "status": "done",
           "detail": f"{answer['how']['queries']} queries · {timings['query']}s"}

    yield {"type": "stage", "stage": "answer", "status": "start"}
    t = time.monotonic()
    m = METRICS[metric_id]
    answer["formula_notes"] = ([_formula_note(metric_id, answer["how"].get("variant") or saved.get(metric_id) or m.default.id,
                                              ctx, metric_id in saved)] if m.ambiguous else [])
    answer["terms"] = glossary.abbreviations_in(answer["summary"] + " " + " ".join(x["text"] for x in answer["rca"]["why"]))
    if use_llm_summary and config.RCA_LLM_REWORD and llm.enabled and left() > 8 and answer.get("facts"):
        allowed = numbers_in(answer["facts"])
        text = await llm.explain(question, answer["facts"], allowed, timeout=min(12.0, max(3.0, left() - 5)))
        if text:
            answer["summary"] = text
            answer["summary_by"] = "llm"
    answer.pop("facts", None)
    timings["answer"] = round(time.monotonic() - t, 2)
    yield {"type": "stage", "stage": "answer", "status": "done"}
    yield answer


def _plan_jobs(plan: Plan, filters: list[Filter], members: list[Filter], today: date, ctx: CompanyCtx) -> list[Job]:
    jobs: list[Job] = []
    p = plan.period
    if plan.shape == "trend":
        grain = plan.grain or ("month" if p.preset in ("last_n_months", "ytd", "last_year") else grain_for(p))
        plan.grain = grain
        for m in plan.metrics:
            jobs.append(Job(m, m, [grain], p, filters))
    elif plan.shape in ("breakdown", "ranking"):
        dims = plan.dims or ["zone"]
        order = plan.order if plan.shape == "ranking" else None
        limit = None
        if plan.shape == "ranking":
            limit = plan.limit or 10
            if plan.extreme and plan.limit is None and dims[0] in ("zone", "region", "day"):
                limit = 40
        for i, m in enumerate(plan.metrics):
            pct = METRICS[m].unit == "percent"
            if i == 0:
                jobs.append(Job(m, m, dims, p, filters, order=order, limit=limit,
                                min_den=20 if pct and plan.shape == "ranking" else None))
            else:
                jobs.append(Job(m, m, dims, p, filters, key_in=None))  # filled after the first returns
    elif plan.shape == "compare_periods":
        periods = plan.compare_periods or [p, previous_period(p, today, ctx.fy_start_month)]
        plan.compare_periods = periods
        for m in plan.metrics[:1]:
            for per in periods:
                jobs.append(Job(per.label, m, [], per, filters))
    elif plan.shape == "compare_members":
        for m in plan.metrics[:1]:
            for mem in members:
                jobs.append(Job(mem.label, m, [], p, filters + [mem]))
    else:
        for m in plan.metrics:
            jobs.append(Job(m, m, [], p, filters))
    return jobs


async def _run_jobs(session, jobs: list[Job], plan: Plan, ctx: CompanyCtx, saved: dict[str, str]) -> dict:
    used_variants: dict[str, str] = {}
    notes: list[str] = []
    sqls: list[str] = []
    ignored: list[str] = []

    def prepare(job: Job):
        check_dims = [k for k in job.keys] + [f.dim for f in job.filters]
        variant, note = pick_variant(job.metric_id, saved.get(job.metric_id), check_dims)
        if note and note not in notes:
            notes.append(note)
        used_variants[job.metric_id] = variant.id
        built = build_metric(ctx.id, variant, job.keys, job.period, job.filters, tz_offset_minutes=ctx.tz_offset,
                             order=job.order, limit=job.limit, key_in=job.key_in, min_denominator=job.min_den)
        for f in built.ignored_filters:
            msg = (f"The {DIM_LABELS.get(f.dim, f.dim).lower()} filter ({f.label}) can't be applied to "
                   f"{METRICS[job.metric_id].label.lower()}, so that figure covers everything.")
            if msg not in ignored:
                ignored.append(msg)
        sqls.append(built.sql)
        return built

    first_dependent = [j for j in jobs if j.key_in is None and plan.shape in ("breakdown", "ranking") and j is not jobs[0]]
    independent = [j for j in jobs if j not in first_dependent] if plan.shape == "ranking" else jobs
    if plan.shape != "ranking":
        first_dependent = []

    results: dict[str, list[dict]] = {}
    built = [(j, prepare(j)) for j in independent]
    rows_list = await asyncio.gather(*[_query(session, b.sql, ctx.id, timeout=config.BUDGET_QUERY) for _, b in built])
    for (j, _), rows in zip(built, rows_list):
        results[j.tag] = rows

    if first_dependent:
        top_rows = results[jobs[0].tag]
        dim = jobs[0].keys[0]
        if dim in ("state", "city"):
            key_filter = Filter(dim, names=[str(r["k0"]) for r in top_rows if r.get("k0")], label="top items")
        elif dim in TIME_GRAINS:
            key_filter = None
        else:
            key_filter = Filter(dim, ids=[int(r["k0"]) for r in top_rows if r.get("k0") is not None], label="top items")
        if key_filter and (key_filter.ids or key_filter.names):
            for j in first_dependent:
                j.key_in = key_filter
            built = [(j, prepare(j)) for j in first_dependent]
            rows_list = await asyncio.gather(*[_query(session, b.sql, ctx.id, timeout=config.BUDGET_QUERY) for _, b in built])
            for (j, _), rows in zip(built, rows_list):
                results[j.tag] = rows
    return {"results": results, "jobs": jobs, "variants": used_variants, "notes": notes + ignored,
            "sqls": sqls, "queries": len(sqls)}


def _compose(plan: Plan, ctx: CompanyCtx, outcome: dict, question: str) -> dict:
    jobs: list[Job] = outcome["jobs"]
    results = outcome["results"]
    metric_ids = list(dict.fromkeys(j.metric_id for j in jobs))
    keys = jobs[0].keys if jobs else []
    rows: list[dict] = []

    if plan.shape in ("compare_periods", "compare_members"):
        m0 = metric_ids[0]
        for j in jobs:
            r = (results.get(j.tag) or [{}])[0]
            rows.append({"label": j.tag if plan.shape == "compare_members" else j.period.label, m0: _num(r.get("v"))})
        keys = ["period" if plan.shape == "compare_periods" else (plan.dims[0] if plan.dims else "member")]
    elif not keys:
        row: dict[str, Any] = {"label": "Total"}
        for j in jobs:
            r = (results.get(j.tag) or [{}])
            row[j.metric_id] = _num(r[0].get("v")) if r else None
        rows.append(row)
    else:
        index: dict[tuple, dict] = {}
        order: list[tuple] = []
        for j in jobs:
            for r in results.get(j.tag, []):
                k = tuple(r.get(f"k{i}") for i in range(len(keys)))
                if k not in index:
                    labels = [_key_label(r, i, key) for i, key in enumerate(keys)]
                    index[k] = {"label": " · ".join(labels), "labels": labels,
                                "id": r.get("k0") if keys[0] not in ("state", "city") and keys[0] not in TIME_GRAINS else None}
                    order.append(k)
                index[k][j.metric_id] = _num(r.get("v"))
        rows = [index[k] for k in order]
        # Daily trend: list days with no data at all
        if keys[0] == "day" and plan.shape == "trend" and plan.period:
            have = {str(k[0])[:10] for k in order}
            missing = []
            d = plan.period.start
            while d <= min(plan.period.end, ctx.today):
                if d.isoformat() not in have:
                    missing.append(d.strftime("%d %b").lstrip("0"))
                d += timedelta(days=1)
            if missing:
                plan.notes.append("No data was recorded for: " + ", ".join(missing[:10]) + ("..." if len(missing) > 10 else "") + ".")

    period_label = plan.period.label if plan.period else ""
    if plan.shape == "compare_periods":
        period_label = " vs ".join(p.label for p in plan.compare_periods)
    snapshot = all(SOURCES[c.source].date_expr is None for m in metric_ids
                   for c in METRICS[m].variant(outcome["variants"].get(m)).components.values())
    if snapshot:
        period_label = f"As of today ({fmt_date(ctx.today)})"
        plan.notes = [n for n in plan.notes if "No period given" not in n]
    filters_label = ", ".join(f"{DIM_LABELS.get(f.dim, f.dim)} {f.label}" for f in _resolved_filters(jobs, plan))
    highlight = rows[0]["label"] if plan.extreme and rows and keys and plan.shape == "ranking" else None
    chart_type = _choose_chart(plan, metric_ids, rows, keys, question)
    facts, summary, headline = _facts_and_summary(plan, ctx, metric_ids, rows, keys, period_label, filters_label, highlight)
    if plan.show_ids and rows and rows[0].get("id") is not None and keys and keys[0] in ("rep", "distributor", "outlet", "sku"):
        facts += f"\nID of {rows[0]['label']}: {rows[0]['id']}"

    empty = all(all(r.get(m) in (None, 0) for m in metric_ids) for r in rows) if rows else True
    if empty:
        summary = (f"I found no {', '.join(METRICS[m].label.lower() for m in metric_ids)}"
                   f"{' for ' + filters_label if filters_label else ''} in {period_label}. "
                   + ("Today's data may still be syncing from the field." if plan.period and plan.period.preset == "today" else ""))
        facts = ""

    saved = prefs.load(ctx.id)
    formula_notes = [_formula_note(m, outcome["variants"].get(m, METRICS[m].default.id), ctx, m in saved)
                     for m in metric_ids if METRICS[m].ambiguous]
    series = [{"id": m, "label": METRICS[m].label, "unit": METRICS[m].unit} for m in metric_ids]
    columns = [{"key": "label", "label": " · ".join(DIM_LABELS.get(k, k.title()) for k in keys) if keys else ""}]
    if plan.show_ids and keys and keys[0] in ("rep", "distributor", "outlet", "sku"):
        columns.append({"key": "id", "label": f"{DIM_LABELS[keys[0]]} ID"})
    columns += [{"key": m, "label": METRICS[m].label, "unit": METRICS[m].unit} for m in metric_ids]
    terms = []
    for m in metric_ids:
        tm = glossary.by_metric(m)
        if tm and tm.term.lower() != tm.full_form.lower():
            terms.append({"term": tm.term, "full_form": tm.full_form})
    for extra in glossary.abbreviations_in(summary + " " + period_label):
        if extra["term"] not in [x["term"] for x in terms]:
            terms.append(extra)

    return {
        "kind": "data" if not empty else "empty",
        "summary": summary,
        "summary_by": "template",
        "headline": headline,
        "facts": facts,
        "chart": None if empty else {"type": chart_type, "series": series, "data": rows, "highlight": highlight,
                                     "x_label": DIM_LABELS.get(keys[0], "") if keys else ""},
        "table": None if empty else {"columns": columns, "rows": rows},
        "formula_notes": formula_notes,
        "notes": list(dict.fromkeys(plan.notes + outcome["notes"])),
        "terms": terms,
        "suggestions": _follow_ups(plan, metric_ids),
        "how": {
            "period": period_label,
            "filters": filters_label or "None",
            "metrics": [{"label": METRICS[m].label, "formula": METRICS[m].variant(outcome["variants"].get(m)).formula,
                         "source": ", ".join(sorted({SOURCES[c.source].description for c in
                                                     METRICS[m].variant(outcome["variants"].get(m)).components.values()}))}
                        for m in metric_ids],
            "interpreted_by": "local LLM" if plan.source == "llm" else "rules",
            "queries": outcome["queries"],
            "sql": outcome["sqls"],
        },
    }


def _resolved_filters(jobs: list[Job], plan: Plan) -> list[Filter]:
    if not jobs:
        return []
    fs = jobs[0].filters
    if plan.shape == "compare_members":
        fs = [f for f in fs if f not in fs[-1:]] if len(fs) > 1 else []
    return fs


def _follow_ups(plan: Plan, metric_ids: list[str]) -> list[str]:
    m = METRICS[metric_ids[0]].label
    out = []
    if plan.shape == "value":
        out = [f"{m} by zone", f"{m} trend for last 6 months", f"Compare this month vs last month {m.lower()}"]
    elif plan.shape == "trend":
        out = [f"{m} by zone", f"{m} by region for MTD"]
    elif plan.shape in ("breakdown", "ranking"):
        out = [f"{m} trend for last 6 months", f"Bottom 5 territories by {m.lower()}"]
    return out[:3]


def _dim_word(dim: str) -> str:
    label = DIM_LABELS.get(dim, dim)
    return label if label.isupper() else label.lower()


def plural(word: str, n: int) -> str:
    if n == 1 or word.endswith("s"):
        return word
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def fmt_date(d: date) -> str:
    return d.strftime("%d %b %Y").lstrip("0")
