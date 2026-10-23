"""Builds ClickHouse SQL from the catalog. The LLM never writes SQL.

Shape of every query (each table sits alone in its own SELECT, filtered by the
locked company id, which is what `guard.py` verifies before anything runs):

    SELECT a.*, n0.name AS k0_name                         -- names, if needed
    FROM (
        SELECT <keys>, <aggregate> AS v
        FROM (SELECT *, ... FROM <table> WHERE <company> = <id> AND <date> ...) AS s
        LEFT JOIN (SELECT ... FROM <outlet master> WHERE <company> = <id>) AS l ...
        WHERE <component filter> GROUP BY <keys>
    ) AS a
    LEFT JOIN (SELECT ... FROM <lookup master> WHERE <company> = <id>) AS n0 ON a.k0 = n0._id
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import (
    DISTRIBUTOR_MASTER, LOOKUPS, OUTLET_MASTER, SOURCES, TIME_GRAINS, Component, Source, Variant,
)
from .timeparse import Period


@dataclass
class Filter:
    dim: str
    ids: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    label: str = ""


@dataclass
class BuiltQuery:
    sql: str
    keys: list[str]  # dims / grains, in column order k0, k1...
    ignored_filters: list[Filter]


def _q(value: str) -> str:
    """Quote a string literal for ClickHouse."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ints(ids: list[int]) -> str:
    return ", ".join(str(int(i)) for i in ids)


def _names(names: list[str]) -> str:
    return ", ".join(_q(n.strip().lower()) for n in names)


def _date_bucket(grain: str) -> str:
    d = "makeDate(intDiv(s._dk, 10000), intDiv(s._dk, 100) % 100, s._dk % 100)"
    if grain == "day":
        return f"toString({d})"
    if grain == "week":
        return f"toString(toMonday({d}))"
    return "toString(intDiv(s._dk, 100))"


def _geo_master(src: Source) -> tuple[str, str, str, dict[str, str], str] | None:
    """(table, id col, company col, {state, city} cols, source column) for state/city lookups."""
    if src.outlet_col:
        table, id_col, company_col, cols = OUTLET_MASTER
        return table, id_col, company_col, cols, src.outlet_col
    if src.distributor_col:
        table, id_col, company_col, cols = DISTRIBUTOR_MASTER
        return table, id_col, company_col, cols, src.distributor_col
    return None


def build_component(
    company_id: int,
    component: Component,
    keys: list[str],
    period: Period | None,
    filters: list[Filter],
    *,
    tz_offset_minutes: int = 330,
    order: str | None = None,
    limit: int | None = None,
    key_in: Filter | None = None,
    with_names: bool = True,
) -> BuiltQuery:
    cid = int(company_id)
    src = SOURCES[component.source]

    # ---- inner: the only place the fact table appears
    extra_cols = []
    if src.array_join:
        extra_cols.append(f"arrayJoin({src.array_join}) AS OutletId")
    if src.date_expr:
        extra_cols.append(f"{src.date_expr} AS _dk")
    inner_where = [f"{src.company_col} = {cid}"]
    if src.date_expr and period is not None:
        inner_where.append(f"{src.date_expr} BETWEEN {period.start_key} AND {period.end_key}")
    if src.base_where:
        inner_where.append(f"({src.base_where})")

    outer_where = []
    if component.where:
        outer_where.append(f"({component.where})")

    ignored: list[Filter] = []
    all_filters = list(filters) + ([key_in] if key_in else [])
    geo = _geo_master(src)
    for f in all_filters:
        if f.dim in ("state", "city"):
            if f.dim in src.dims:  # outlet master itself
                outer_where.append(f"lower(trimBoth(s.{src.dims[f.dim]})) IN ({_names(f.names)})")
            elif geo:
                table, id_col, company_col, cols, src_col = geo
                outer_where.append(
                    f"toInt64(s.{src_col}) IN (SELECT toInt64({id_col}) FROM {table} FINAL "
                    f"WHERE {company_col} = {cid} AND lower(trimBoth({cols[f.dim]})) IN ({_names(f.names)}))"
                )
            else:
                ignored.append(f)
        elif f.dim in src.dims and f.ids:
            outer_where.append(f"toInt64(s.{src.dims[f.dim]}) IN ({_ints(f.ids)})")
        else:
            ignored.append(f)

    final = " FINAL" if src.final else ""
    select_inner = "*" + "".join(f", {c}" for c in extra_cols)
    inner = (f"SELECT {select_inner} FROM {src.table}{final} "
             f"WHERE {' AND '.join(inner_where)}")

    # ---- keys and joins for state/city
    key_exprs: list[str] = []
    joins: list[str] = []
    geo_joined = False
    for i, k in enumerate(keys):
        if k in TIME_GRAINS:
            key_exprs.append(f"{_date_bucket(k)} AS k{i}")
        elif k in ("state", "city"):
            if k in src.dims:
                key_exprs.append(f"initcapUTF8(lower(trimBoth(s.{src.dims[k]}))) AS k{i}")
            elif geo:
                table, id_col, company_col, cols, src_col = geo
                if not geo_joined:
                    joins.append(
                        f"LEFT JOIN (SELECT toInt64({id_col}) AS _gid, any({cols['state']}) AS _state, "
                        f"any({cols['city']}) AS _city FROM {table} FINAL WHERE {company_col} = {cid} "
                        f"GROUP BY _gid) AS g ON toInt64(s.{src_col}) = g._gid"
                    )
                    geo_joined = True
                key_exprs.append(f"initcapUTF8(lower(trimBoth(g._{k}))) AS k{i}")
            else:
                raise ValueError(f"{src.id} cannot be split by {k}")
        elif k in src.dims:
            key_exprs.append(f"toInt64(s.{src.dims[k]}) AS k{i}")
        else:
            raise ValueError(f"{src.id} cannot be split by {k}")

    expr = component.expr.format(tz_seconds=int(tz_offset_minutes) * 60)
    select = ", ".join(key_exprs + [f"{expr} AS v"])
    sql = f"SELECT {select} FROM ({inner}) AS s"
    if joins:
        sql += " " + " ".join(joins)
    if outer_where:
        sql += " WHERE " + " AND ".join(outer_where)
    if keys:
        sql += " GROUP BY " + ", ".join(f"k{i}" for i in range(len(keys)))
    if order and keys:
        sql += f" ORDER BY v {'ASC' if order == 'asc' else 'DESC'}"
        if limit:
            sql += f" LIMIT {int(limit)}"

    # ---- names for id keys
    name_joins = []
    name_cols = []
    for i, k in enumerate(keys):
        if with_names and k in LOOKUPS:
            table, id_col, name_col, company_col = LOOKUPS[k]
            name_joins.append(
                f"LEFT JOIN (SELECT toInt64({id_col}) AS _id, any({name_col}) AS name FROM {table} FINAL "
                f"WHERE {company_col} = {cid} GROUP BY _id) AS n{i} ON a.k{i} = n{i}._id"
            )
            name_cols.append(f"n{i}.name AS k{i}_name")
    if name_joins:
        sql = f"SELECT a.*, {', '.join(name_cols)} FROM ({sql}) AS a " + " ".join(name_joins)
        if order and keys:
            sql += f" ORDER BY a.v {'ASC' if order == 'asc' else 'DESC'}"

    return BuiltQuery(sql=sql, keys=list(keys), ignored_filters=ignored)


def build_metric(
    company_id: int,
    variant: Variant,
    keys: list[str],
    period: Period | None,
    filters: list[Filter],
    *,
    tz_offset_minutes: int = 330,
    order: str | None = None,
    limit: int | None = None,
    key_in: Filter | None = None,
    min_denominator: int | None = None,
) -> BuiltQuery:
    """One SQL per metric: components joined on their keys, the formula applied, names attached.

    Output columns: k0.., p_<component>.., v, k0_name..
    """
    cid = int(company_id)
    parts = []
    ignored: list[Filter] = []
    for name, comp in variant.components.items():
        q = build_component(cid, comp, keys, period, filters, tz_offset_minutes=tz_offset_minutes,
                            key_in=key_in, with_names=False)
        ignored += [f for f in q.ignored_filters if f not in ignored]
        parts.append((name, q.sql))

    key_cols = [f"k{i}" for i in range(len(keys))]
    if len(parts) == 1:
        name, sql = parts[0]
        joined = f"SELECT {''.join(k + ', ' for k in key_cols)}v AS p_{name} FROM ({sql}) AS c0"
    else:
        selects = []
        froms = ""
        for i, (name, sql) in enumerate(parts):
            if i == 0:
                froms = f"({sql}) AS c0"
            elif key_cols:
                froms += f" FULL OUTER JOIN ({sql}) AS c{i} USING ({', '.join(key_cols)})"
            else:
                froms += f" CROSS JOIN ({sql}) AS c{i}"
            selects.append(f"c{i}.v AS p_{name}")
        joined = f"SELECT {''.join(k + ', ' for k in key_cols)}{', '.join(selects)} FROM {froms}"

    if variant.combine is not None:
        value_expr = variant.combine.sql()
    else:
        value_expr = f"toFloat64(p_{parts[0][0]})"
    sql = f"SELECT j.*, {value_expr} AS v FROM ({joined}) AS j"

    name_joins, name_cols = [], []
    for i, k in enumerate(keys):
        if k in LOOKUPS:
            table, id_col, name_col, company_col = LOOKUPS[k]
            name_joins.append(
                f"LEFT JOIN (SELECT toInt64({id_col}) AS _id, any({name_col}) AS name FROM {table} FINAL "
                f"WHERE {company_col} = {cid} GROUP BY _id) AS n{i} ON a.k{i} = n{i}._id"
            )
            name_cols.append(f"n{i}.name AS k{i}_name")
    sql = f"SELECT a.*{''.join(', ' + c for c in name_cols)} FROM ({sql}) AS a " + " ".join(name_joins)
    conds = []
    if min_denominator and variant.combine is not None:
        conds.append(f"toFloat64(a.p_{variant.combine.den}) >= {int(min_denominator)}")
    if keys and order:
        conds.append("a.v IS NOT NULL")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    if keys:
        if order:
            sql += f" ORDER BY a.v {'ASC' if order == 'asc' else 'DESC'}"
            if limit:
                sql += f" LIMIT {int(limit)}"
        elif any(k in TIME_GRAINS for k in keys):
            sql += " ORDER BY " + ", ".join(f"a.k{i}" for i in range(len(keys)))
        else:
            sql += " ORDER BY a.v DESC"
    return BuiltQuery(sql=sql.strip(), keys=list(keys), ignored_filters=ignored)


def build_lookup_search(company_id: int, terms: list[str], dims: list[str]) -> str:
    """One query that looks up candidate names across this company's masters.

    A term matches a name exactly, or as the start of a name followed by " (" so
    "pure" finds "Pure", "Pure (F)" and "Pure (B)".
    """
    cid = int(company_id)
    parts = []
    names = _names(terms)
    prefixes = " OR ".join(f"startsWith(lower(trimBoth({{col}})), {_q(t.strip().lower() + ' (')})" for t in terms)
    for dim in dims:
        if dim in ("state", "city"):
            table, _, company_col, cols = OUTLET_MASTER
            col = cols[dim]
            cond = f"lower(trimBoth({col})) IN ({names})"
            parts.append(
                f"SELECT {_q(dim)} AS dim, toInt64(0) AS id, initcapUTF8(any(trimBoth({col}))) AS name, "
                f"lower(trimBoth({col})) AS term FROM {table} FINAL WHERE {company_col} = {cid} "
                f"AND {cond} GROUP BY term"
            )
        elif dim in LOOKUPS:
            table, id_col, name_col, company_col = LOOKUPS[dim]
            cond = f"(lower(trimBoth({name_col})) IN ({names}) OR {prefixes.format(col=name_col)})"
            parts.append(
                f"SELECT {_q(dim)} AS dim, toInt64({id_col}) AS id, any({name_col}) AS name, "
                f"lower(trimBoth(any({name_col}))) AS term FROM {table} FINAL WHERE {company_col} = {cid} "
                f"AND {cond} GROUP BY id"
            )
    return " UNION ALL ".join(parts)


def build_member_list(company_id: int, dim: str, limit: int = 40) -> str:
    """Names a user can pick from when they typed a placeholder like 'xyz zone'."""
    cid = int(company_id)
    if dim in ("state", "city"):
        table, _, company_col, cols = OUTLET_MASTER
        col = cols[dim]
        return (f"SELECT trimBoth({col}) AS name, count() AS n FROM {table} FINAL WHERE {company_col} = {cid} "
                f"AND IsBlocked = 0 AND trimBoth({col}) != '' GROUP BY name ORDER BY n DESC LIMIT {int(limit)}")
    table, id_col, name_col, company_col = LOOKUPS[dim]
    return (f"SELECT any({name_col}) AS name, toInt64({id_col}) AS id FROM {table} FINAL "
            f"WHERE {company_col} = {cid} GROUP BY id ORDER BY name LIMIT {int(limit)}")
