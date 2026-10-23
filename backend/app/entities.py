"""Resolves names typed by the user ("North East", "Karnataka", "Pure") to this company's own
master records. Lookups only ever search the selected company's masters."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import DIM_LABELS, GEO_DIMS
from .interpreter import RawFilter
from .sqlbuild import Filter, build_lookup_search

SEARCH_ORDER = list(GEO_DIMS) + ["category", "brand", "distributor"]


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
    terms = sorted({f.text.strip().lower() for f in wanted})
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
            hits = [r for r in rows if r["dim"] == dim and _match(term, str(r["name"]))]
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

    for raw in filters:
        f = pick(raw)
        if f:
            out.filters.append(f)
        else:
            out.unresolved.append(raw)
    for raw in members:
        f = pick(raw)
        if f:
            out.members.append(f)
        else:
            out.unresolved.append(raw)
    return out
