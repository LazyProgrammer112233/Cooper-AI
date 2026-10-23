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


# --------------------------------------------------------------------------- master lists


@dataclass(frozen=True)
class ListSpec:
    table: str
    id_col: str
    name_col: str
    company_col: str
    active: str  # condition for active, non-deleted rows
    parents: dict[str, str] = field(default_factory=dict)  # filter dim -> column in this master
    extras: tuple[tuple[str, str, str], ...] = ()  # (key, label, expression)
    child: str | None = None  # what the per-item count counts ("SKUs", "regions", ...)


LIST_SPECS: dict[str, ListSpec] = {
    "zone": ListSpec("master_facompanyzones", "Id", "Name", "Company", "IsDeactive = 0", child="regions"),
    "region": ListSpec("master_regions", "Id", "Name", "CompanyId", "Deleted = 0 AND IsDeactive = 0",
                       parents={"zone": "ZoneId"}, child="territories"),
    "territory": ListSpec("master_territories", "Id", "Name", "CompanyId", "Deleted = 0 AND IsDeactive = 0",
                          parents={"zone": "CompanyZoneId", "region": "RegionId"}),
    "brand": ListSpec("master_faproductdivision", "Id", "Name", "CompanyId", "Deleted = 0 AND IsDeactive = 0", child="SKUs"),
    "distributor": ListSpec("master_fadistributors", "Id", "Name", "CompanyId", "Deleted = 0 AND IsDeactive = 0",
                            parents={"zone": "ZoneId", "region": "RegionId", "state": "State", "city": "CityName"},
                            extras=(("city", "City", "CityName"), ("state", "State", "State"))),
    "rep": ListSpec("master_clientemployees", "Id", "Name", "Company",
                    "Deleted = 0 AND IsDeleted = 0 AND IsFieldAppuser = 1 AND IsTrainingUser = 0",
                    parents={"zone": "CompanyZoneId", "region": "RegionId"}),
    "outlet": ListSpec("master_f2klocations", "ID", "ShopName", "Company", "IsBlocked = 0",
                       parents={"state": "State", "city": "City"},
                       extras=(("city", "City", "City"), ("state", "State", "State"))),
}
LISTABLE = tuple(LIST_SPECS) + ("category", "sku", "state", "city")


def _parent_cond(col: str, f: Filter) -> str:
    if f.names:
        return f"lower(trimBoth({col})) IN ({_names(f.names)})"
    return f"{col} IN ({_ints(f.ids)})"


def _product_chain(cid: int) -> str:
    """Active SKUs with their primary category and brand (SKU -> secondary category -> primary category -> brand)."""
    return (
        f"(SELECT toInt64(ID) AS sku, any(Name) AS sku_name, any(MRP) AS mrp, toInt64(any(ProductCategoryId)) AS sc "
        f"FROM master_facompanyproducts FINAL WHERE CompanyId = {cid} AND Deleted = 0 AND IsActive = 1 GROUP BY sku) AS s "
        f"LEFT JOIN (SELECT toInt64(Id) AS sc_id, toInt64(any(ProductPrimaryCategoryId)) AS pc "
        f"FROM master_faproductsecondarycategory FINAL WHERE CompanyId = {cid} GROUP BY sc_id) AS x ON s.sc = x.sc_id "
        f"LEFT JOIN (SELECT toInt64(Id) AS pc_id, any(Name) AS cat_name, toInt64(any(ProductDivisionId)) AS div "
        f"FROM master_faproductprimarycategory FINAL WHERE CompanyId = {cid} GROUP BY pc_id) AS c ON x.pc = c.pc_id "
        f"LEFT JOIN (SELECT toInt64(Id) AS div_id, any(Name) AS brand_name "
        f"FROM master_faproductdivision FINAL WHERE CompanyId = {cid} GROUP BY div_id) AS b ON c.div = b.div_id"
    )


def _child_counts(cid: int, dim: str) -> str:
    """(pid, n) rows: how many active children each item of `dim` has."""
    if dim == "zone":
        return (f"SELECT toInt64(ZoneId) AS pid, count() AS n FROM master_regions FINAL WHERE CompanyId = {cid} "
                f"AND Deleted = 0 AND IsDeactive = 0 GROUP BY pid")
    if dim == "region":
        return (f"SELECT toInt64(RegionId) AS pid, count() AS n FROM master_territories FINAL WHERE CompanyId = {cid} "
                f"AND Deleted = 0 AND IsDeactive = 0 GROUP BY pid")
    return f"SELECT c.div AS pid, count() AS n FROM {_product_chain(cid)} GROUP BY pid"  # brand: active SKUs


def _product_list(cid: int, dim: str, filters: list[Filter], lim: int) -> tuple[str, list[Filter], list[tuple[str, str]]]:
    where, ignored = [], []
    for f in filters:
        if f.dim == "category" and f.ids:
            where.append(f"x.pc IN ({_ints(f.ids)})")
        elif f.dim == "brand" and f.ids:
            where.append(f"c.div IN ({_ints(f.ids)})")
        else:
            ignored.append(f)
    cond = f" WHERE {' AND '.join(where)}" if where else ""
    if dim == "sku":
        sql = (f"SELECT s.sku AS id, s.sku_name AS name, c.cat_name AS category, b.brand_name AS brand, s.mrp AS mrp "
               f"FROM {_product_chain(cid)}{cond} ORDER BY name LIMIT {lim}")
        return sql, ignored, [("category", "Category"), ("brand", "Brand"), ("mrp", "MRP")]
    # Grouped by name: the same category ("Pure") usually exists once per brand.
    sql = (f"SELECT ifNull(c.cat_name, 'Not mapped') AS name, count() AS n, "
           f"arrayStringConcat(arraySort(groupUniqArray(ifNull(b.brand_name, ''))), ', ') AS brands "
           f"FROM {_product_chain(cid)}{cond} GROUP BY name ORDER BY n DESC LIMIT {lim}")
    return sql, ignored, [("brands", "Brands")]


def build_list(company_id: int, dim: str, filters: list[Filter], limit: int = 500) -> tuple[str, list[Filter], list[tuple[str, str]]]:
    """Active items of one master (zones, categories, SKUs, distributors, reps...), with optional parent filters.

    Returns (sql, filters that don't apply to this list, extra columns as (key, label)).
    Rows carry id, name, and n (child count) where the master has children.
    """
    cid = int(company_id)
    lim = int(limit)
    if dim in ("state", "city"):
        table, _, company_col, cols = OUTLET_MASTER
        col = cols[dim]
        where = [f"{company_col} = {cid}", "IsBlocked = 0", f"trimBoth({col}) != ''"]
        ignored = []
        for f in filters:
            if f.dim in ("state", "city") and f.dim != dim:
                where.append(_parent_cond(cols[f.dim], f))
            else:
                ignored.append(f)
        sql = (f"SELECT initcapUTF8(any(trimBoth({col}))) AS name, lower(trimBoth({col})) AS _k, count() AS n "
               f"FROM {table} FINAL WHERE {' AND '.join(where)} GROUP BY _k ORDER BY n DESC LIMIT {lim}")
        return sql, ignored, []

    if dim in ("category", "sku"):
        return _product_list(cid, dim, filters, lim)
    spec = LIST_SPECS[dim]
    where = [f"{spec.company_col} = {cid}", spec.active]
    ignored: list[Filter] = []
    for f in filters:
        if f.dim in spec.parents:
            where.append(_parent_cond(spec.parents[f.dim], f))
        else:
            ignored.append(f)
    extra_sel = "".join(f", any({expr}) AS {key}" for key, _, expr in spec.extras)
    inner = (f"SELECT toInt64({spec.id_col}) AS id, any({spec.name_col}) AS name{extra_sel} FROM {spec.table} FINAL "
             f"WHERE {' AND '.join(where)} GROUP BY id")
    extras = [(key, label) for key, label, _ in spec.extras]
    if spec.child:
        sql = (f"SELECT i.id AS id, i.name AS name, c.n AS n{''.join(f', i.{k} AS {k}' for k, _ in extras)} "
               f"FROM ({inner}) AS i LEFT JOIN ({_child_counts(cid, dim)}) AS c ON i.id = c.pid ORDER BY name LIMIT {lim}")
        return sql, ignored, extras
    return f"SELECT * FROM ({inner}) AS i ORDER BY name LIMIT {lim}", ignored, extras
