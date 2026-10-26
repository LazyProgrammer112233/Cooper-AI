"""Resolves names typed by the user ("North East", "Karnataka", "Pure") to this company's own
master records. Lookups only ever search the selected company's masters."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .catalog import DIM_LABELS, GEO_DIMS
from .interpreter import RawFilter
from .sqlbuild import Filter, build_lookup_search, build_member_list

SEARCH_ORDER = list(GEO_DIMS) + ["brand", "category", "distributor"]  # a name that is both a brand and a category is the brand


@dataclass
class Resolution:
    filters: list[Filter] = field(default_factory=list)
    members: list[Filter] = field(default_factory=list)
    unresolved: list[RawFilter] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _dims_for(raw: RawFilter) -> list[str]:
    if raw.dim_hint in ("sku", "outlet", "rep"):
        return [raw.dim_hint]
    if raw.dim_hint:
        rest = [d for d in SEARCH_ORDER if d != raw.dim_hint]
        return [raw.dim_hint] + rest
    return SEARCH_ORDER


def _match(term: str, name: str) -> bool:
    n = name.strip().lower()
    return n == term or n.startswith(term + " (")


async def resolve(session, company_id: int, filters: list[RawFilter], members: list[RawFilter]) -> Resolution:
    out = Resolution()
    wanted = [f for f in filters + members if f.text]
    if not wanted:
        return out
    terms = sorted({f.text.strip().lower() for f in wanted} | {ALIASES[f.text.strip().lower()] for f in wanted
                                                               if f.text.strip().lower() in ALIASES})
    dims = []
    for f in wanted:
        for d in _dims_for(f):
            if d not in dims:
                dims.append(d)
    sql = build_lookup_search(company_id, terms, dims)
    from .guard import check_sql

    check_sql(sql, company_id)
    result = await session.ch_query(sql, top=500, timeout=12)
    rows = result.rows

    def pick(raw: RawFilter) -> Filter | None:
        term = raw.text.strip().lower()
        for dim in _dims_for(raw):
            hits = [r for r in rows if r["dim"] == dim and (_match(term, str(r["name"])) or
                                                             (term in ALIASES and _match(ALIASES[term], str(r["name"]))))]
            if not hits:
                continue
            exact = [h for h in hits if str(h["name"]).strip().lower() == term]
            names = sorted({str(h["name"]).strip() for h in hits})
            label = names[0] if len(names) == 1 else ", ".join(names[:4]) + ("..." if len(names) > 4 else "")
            if raw.dim_hint and dim != raw.dim_hint:
                out.notes.append(f"I couldn't find a {DIM_LABELS[raw.dim_hint].lower()} called "
                                 f"\"{raw.text.title()}\", so I used the {DIM_LABELS[dim].lower()} \"{label}\".")
            if len(names) > 1 and not exact:
                out.notes.append(f"\"{raw.text.title()}\" matched several {DIM_LABELS[dim].lower()} names: {label}. I included all of them.")
            if dim in ("state", "city"):
                return Filter(dim, names=[str(h["name"]) for h in hits], label=label)
            return Filter(dim, ids=sorted({int(h["id"]) for h in hits}), label=label)
        return None

    async def pick_or_near(raw: RawFilter) -> Filter | None:
        f = pick(raw)
        if f is None:
            f = await _near_miss(session, company_id, raw, out.notes)
        return f

    for raw in filters:
        f = await pick_or_near(raw)
        if f:
            out.filters.append(f)
        else:
            out.unresolved.append(raw)
    for raw in members:
        f = await pick_or_near(raw)
        if f:
            out.members.append(f)
        else:
            out.unresolved.append(raw)
    return out


# --------------------------------------------------------------------------- near misses

ALIASES = {"up": "uttar pradesh", "mp": "madhya pradesh", "ap": "andhra pradesh", "tn": "tamil nadu", "wb": "west bengal",
           "hp": "himachal pradesh", "uk": "uttarakhand", "jk": "jammu and kashmir", "j&k": "jammu and kashmir",
           "cg": "chhattisgarh", "ncr": "delhi", "blr": "bengaluru", "bangalore": "bengaluru", "bombay": "mumbai",
           "calcutta": "kolkata", "madras": "chennai", "gurgaon": "gurugram", "orissa": "odisha"}
NEAR_DIMS = list(GEO_DIMS) + ["category", "brand"]
_MEMBERS: dict[tuple[int, str], tuple[float, list[tuple[str, int | None]]]] = {}


def _lev(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def near(term: str, name: str) -> bool:
    """'north1' ~ 'North', 'karnatak' ~ 'Karnataka', 'up' ~ 'Uttar Pradesh'; never 'earth' ~ 'East'."""
    t, n = term.strip().lower(), name.strip().lower()
    if not t or not n:
        return False
    if ALIASES.get(t) and (n == ALIASES[t] or n.startswith(ALIASES[t] + " ")):
        return True
    base = re.sub(r"[\s\-_]*(\d+|zone|region|area|hq)$", "", t).strip()
    if base and base != t and (base == n or ALIASES.get(base) == n):
        return True
    if len(t) < 4:
        return False
    limit = 1 if len(t) <= 6 else 2
    return t[0] == n[0] and _lev(t, n, limit) <= limit


async def _members(session, company_id: int, dim: str) -> list[tuple[str, int | None]]:
    key = (company_id, dim)
    hit = _MEMBERS.get(key)
    if hit and time.monotonic() - hit[0] < 1800:
        return hit[1]
    from .guard import check_sql
    sql = build_member_list(company_id, dim, limit=500)
    check_sql(sql, company_id)
    rows = (await session.ch_query(sql, top=500, timeout=10)).rows
    names = [(str(r.get("name") or "").strip(), r.get("id")) for r in rows if str(r.get("name") or "").strip()]
    _MEMBERS[key] = (time.monotonic(), names)
    return names


async def _near_miss(session, company_id: int, raw: RawFilter, notes: list[str]) -> Filter | None:
    """One clearly close real name -> use it and say so. None, or several, -> None (the caller refuses to guess)."""
    dims = [raw.dim_hint] if raw.dim_hint in NEAR_DIMS else ([] if raw.dim_hint else NEAR_DIMS)
    found: list[tuple[str, str, int | None]] = []
    for dim in dims:
        try:
            for name, mid in await _members(session, company_id, dim):
                if near(raw.text, name):
                    found.append((dim, name, mid))
        except Exception:
            continue
    names = {(d, n.lower()) for d, n, _ in found}
    if len(names) != 1:
        return None
    dim, name, _ = found[0]
    word = DIM_LABELS[dim].lower()
    notes.append(f"There is no {word} called \"{raw.text.title()}\", so I used the {word} \"{name}\".")
    if dim in ("state", "city"):
        return Filter(dim, names=[n for d, n, _ in found], label=name)
    return Filter(dim, ids=sorted({int(m) for _, _, m in found if m is not None}), label=name)
