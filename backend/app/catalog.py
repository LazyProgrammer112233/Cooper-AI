"""Metric catalog: the single place where every metric Cooper can answer is defined.

The LLM never writes SQL. It (or the rule parser) picks metrics, breakdowns,
filters and a period from this catalog, and `sqlbuild.py` turns that into SQL.

Formulas mirror Pulse's own KPI definitions (PulseKpiConfigurations in
Masters_GT_Replica) where Pulse has one. Metrics that companies define
differently carry up to four formula options; the first one is the basic
formula Cooper uses until the user picks another for their company.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Sources: one ClickHouse table each, always filtered by company and date.
# ---------------------------------------------------------------------------

GEO_DIMS = ("zone", "region", "territory", "state", "city")
PRODUCT_DIMS = ("category", "brand", "sku")
ALL_DIMS = GEO_DIMS + PRODUCT_DIMS + ("distributor", "outlet", "rep")
TIME_GRAINS = ("day", "week", "month")


@dataclass(frozen=True)
class Source:
    id: str
    table: str
    company_col: str
    date_expr: str | None  # expression giving a YYYYMMDD UInt32, None for snapshot tables
    base_where: str = ""
    final: bool = False
    array_join: str | None = None  # explode an array of outlet ids into OutletId
    dims: dict[str, str] = field(default_factory=dict)  # dim -> column in the source row
    outlet_col: str | None = None  # used for state / city via the outlet master
    distributor_col: str | None = None  # used for state / city via the distributor master
    description: str = ""


SDS_DIMS = {
    "zone": "ZoneId", "region": "RegionId", "territory": "TerritoryId",
    "category": "PrimaryCategoryId", "brand": "ProductDivisionId", "sku": "ProductId",
    "distributor": "DistributorId", "outlet": "OutletId", "rep": "FieldUserId",
}

SOURCES: dict[str, Source] = {
    "sds_order": Source(
        "sds_order", "SecondaryDemandSale", "CompanyId", "OrderDateKey",
        "InvoiceId = 0 AND IsOrderCancelled = 0", dims=SDS_DIMS, outlet_col="OutletId",
        description="Outlet visits and orders booked by salesmen",
    ),
    "sds_invoice": Source(
        "sds_invoice", "SecondaryDemandSale", "CompanyId", "InvoiceDateKey",
        "InvoiceId > 0 AND IsInvoiceCancelled = 0", dims=SDS_DIMS, outlet_col="OutletId",
        description="Distributor-to-outlet invoices (secondary billing)",
    ),
    "primary": Source(
        "primary", "PrimaryReprimary", "CompanyId", "InvoiceDateKey",
        "InvoiceId > 0 AND IsInvoiceCancelled = 0",
        dims={
            "zone": "BuyerZoneId", "region": "BuyerRegionId", "category": "PrimaryCategoryId",
            "brand": "ProductDivisionId", "sku": "ProductId", "distributor": "BuyerId",
        },
        distributor_col="BuyerId",
        description="Company-to-distributor invoices (primary and re-primary billing)",
    ),
    "primary_order": Source(
        "primary_order", "PrimaryReprimary", "CompanyId", "OrderDateKey",
        "PrimaryOrderId > 0 AND IsOrderCancelled = 0",
        dims={
            "zone": "BuyerZoneId", "region": "BuyerRegionId", "category": "PrimaryCategoryId",
            "brand": "ProductDivisionId", "sku": "ProductId", "distributor": "BuyerId",
        },
        distributor_col="BuyerId",
        description="Primary orders placed by distributors",
    ),
    "ejd_assigned": Source(
        "ejd_assigned", "EmployeeJourneyDetail", "CompanyId", "DateKey",
        array_join="AssignedOutletIds",
        dims={"zone": "ZoneId", "region": "RegionId", "territory": "TerritoryId", "rep": "FieldUserId", "outlet": "OutletId"},
        outlet_col="OutletId",
        description="Outlets on the salesmen's assigned (planned) beats each day",
    ),
    "ejd_mapped": Source(
        "ejd_mapped", "EmployeeJourneyDetail", "CompanyId", "DateKey",
        array_join="MappedOutletIds",
        dims={"zone": "ZoneId", "region": "RegionId", "territory": "TerritoryId", "rep": "FieldUserId", "outlet": "OutletId"},
        outlet_col="OutletId",
        description="All outlets on beats mapped to the salesmen",
    ),
    "edp": Source(
        "edp", "EmployeeDayPerformance", "CompanyId", "DayStartDateKey",
        dims={"zone": "ZoneId", "region": "RegionId", "territory": "TerritaryId", "rep": "FieldUserId"},
        description="One row per salesman per working day",
    ),
    "outlets": Source(
        "outlets", "master_f2klocations", "Company", None, final=True,
        dims={"state": "State", "city": "City", "outlet": "ID"},
        description="Outlet master (snapshot, no date)",
    ),
    "outlets_created": Source(
        "outlets_created", "master_f2klocations", "Company", "toUInt32(toYYYYMMDD(CreatedAt))", final=True,
        dims={"state": "State", "city": "City", "outlet": "ID"},
        description="Outlet master, dated by creation date",
    ),
}

# Masters used to turn ids into names. All filtered by company.
LOOKUPS: dict[str, tuple[str, str, str, str]] = {
    # dim: (table, id column, name column, company column)
    "zone": ("master_facompanyzones", "Id", "Name", "Company"),
    "region": ("master_regions", "Id", "Name", "CompanyId"),
    "territory": ("master_territories", "Id", "Name", "CompanyId"),
    "category": ("master_faproductprimarycategory", "Id", "Name", "CompanyId"),
    "brand": ("master_faproductdivision", "Id", "Name", "CompanyId"),
    "sku": ("master_facompanyproducts", "ID", "Name", "CompanyId"),
    "distributor": ("master_fadistributors", "Id", "Name", "CompanyId"),
    "outlet": ("master_f2klocations", "ID", "ShopName", "Company"),
    "rep": ("master_clientemployees", "Id", "Name", "Company"),
}

OUTLET_MASTER = ("master_f2klocations", "ID", "Company", {"state": "State", "city": "City"})
DISTRIBUTOR_MASTER = ("master_fadistributors", "Id", "CompanyId", {"state": "State", "city": "CityName"})

DIM_LABELS = {
    "zone": "Zone", "region": "Region", "territory": "Territory", "state": "State", "city": "City",
    "category": "Category", "brand": "Brand", "sku": "SKU", "distributor": "Distributor",
    "outlet": "Outlet", "rep": "Sales rep", "day": "Day", "week": "Week", "month": "Month",
}


def source_supports(src: Source, dim: str) -> bool:
    if dim in TIME_GRAINS:
        return src.date_expr is not None
    if dim in src.dims:
        return True
    if dim in ("state", "city"):
        return bool(src.outlet_col or src.distributor_col)
    return False


# ---------------------------------------------------------------------------
# Metrics and formula options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    source: str
    expr: str
    where: str = ""


@dataclass(frozen=True)
class Variant:
    id: str
    label: str
    formula: str
    components: dict[str, Component]
    combine: "Ratio | None" = None
    pulse_kpi: str = ""

    def sources(self) -> list[Source]:
        return [SOURCES[c.source] for c in self.components.values()]

    def supports(self, dim: str | None) -> bool:
        if not dim:
            return True
        return all(source_supports(s, dim) for s in self.sources())


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    unit: str  # currency | count | percent | time | number
    variants: tuple[Variant, ...]
    ambiguous: bool = False  # companies commonly use different formulas
    higher_is_better: bool = True
    glossary_term: str = ""

    @property
    def default(self) -> Variant:
        return self.variants[0]

    def variant(self, variant_id: str | None) -> Variant:
        for v in self.variants:
            if v.id == variant_id:
                return v
        return self.default


@dataclass(frozen=True)
class Ratio:
    num: str
    den: str
    scale: float = 100.0

    def __call__(self, values: dict[str, float | None]) -> float | None:
        n, d = values.get(self.num), values.get(self.den)
        if n is None or not d:
            return None
        return n / d * self.scale

    def sql(self) -> str:
        return f"toFloat64(p_{self.num}) * {self.scale} / nullIf(toFloat64(p_{self.den}), 0)"


def ratio(num: str, den: str, scale: float = 100.0) -> Ratio:
    return Ratio(num, den, scale)


def _single(name: str, source: str, expr: str, where: str = "") -> dict[str, Component]:
    return {name: Component(source, expr, where)}


SUM = "toFloat64(sum({}))"


def S(col: str) -> str:
    return SUM.format(col)


ASSIGNED = Component("ejd_assigned", "uniqExact(OutletId)", "OutletId > 0")
MAPPED = Component("ejd_mapped", "uniqExact(OutletId)", "OutletId > 0")
UNIVERSE = Component("outlets", "count()", "IsBlocked = 0")
VISITED = Component("sds_order", "uniqExact(OutletId)", "OutletId > 0")
PRODUCTIVE = Component("sds_order", "uniqExact(OutletId)", "OutletId > 0 AND IsProductive = 1")
BILLED = Component("sds_invoice", "uniqExact(OutletId)", "OutletId > 0")


METRICS: dict[str, Metric] = {}


def _add(m: Metric) -> None:
    METRICS[m.id] = m


_add(Metric("primary_sales", "Primary sales", "currency", (
    Variant("net_invoice", "Net invoice value", "Sum of net invoice value of non-cancelled primary invoices (after discounts, before tax)",
            _single("v", "primary", S("InvoiceNetValue")), pulse_kpi="PrimarySales"),
    Variant("gross_invoice", "Gross invoice value", "Sum of gross invoice value of non-cancelled primary invoices (before discounts)",
            _single("v", "primary", S("InvoiceGrossValue"))),
    Variant("invoice_with_tax", "Invoice value incl. taxes", "Sum of invoice total value (net value + GST) of non-cancelled primary invoices",
            _single("v", "primary", S("InvoiceTotalValue"))),
    Variant("primary_orders", "Primary order value", "Sum of net value of non-cancelled primary orders placed by distributors (booked, not invoiced)",
            _single("v", "primary_order", S("OrderNetValue"))),
), ambiguous=True, glossary_term="Primary Sales"))

_add(Metric("secondary_sales", "Secondary sales", "currency", (
    Variant("net_invoice", "Net invoice value", "Sum of net invoice value of non-cancelled distributor-to-outlet invoices",
            _single("v", "sds_invoice", S("InvoiceNetValue")), pulse_kpi="SecondarySales"),
    Variant("order_value", "Order booking value", "Sum of net value of non-cancelled orders booked by salesmen at outlets",
            _single("v", "sds_order", S("OrderNetValue"), "AttendanceId > 0"), pulse_kpi="OrdersValue"),
    Variant("gross_invoice", "Gross invoice value", "Sum of gross invoice value of non-cancelled secondary invoices (before discounts)",
            _single("v", "sds_invoice", S("InvoiceGrossValue"))),
    Variant("invoice_with_tax", "Invoice value incl. taxes", "Sum of invoice total value (net value + GST) of non-cancelled secondary invoices",
            _single("v", "sds_invoice", S("InvoiceTotalValue"))),
), ambiguous=True, glossary_term="Secondary Sales"))

_add(Metric("primary_invoice_value", "Primary invoice value", "currency", (
    Variant("invoice_with_tax", "Invoice value incl. taxes", "Sum of invoice total value (net value + GST) of non-cancelled primary invoices",
            _single("v", "primary", S("InvoiceTotalValue"))),
    Variant("net_invoice", "Net invoice value", "Sum of net invoice value of non-cancelled primary invoices",
            _single("v", "primary", S("InvoiceNetValue"))),
), glossary_term="Invoice Value"))

_add(Metric("secondary_invoice_value", "Secondary invoice value", "currency", (
    Variant("invoice_with_tax", "Invoice value incl. taxes", "Sum of invoice total value (net value + GST) of non-cancelled secondary invoices",
            _single("v", "sds_invoice", S("InvoiceTotalValue"))),
    Variant("net_invoice", "Net invoice value", "Sum of net invoice value of non-cancelled secondary invoices",
            _single("v", "sds_invoice", S("InvoiceNetValue"))),
), glossary_term="Invoice Value"))

_add(Metric("order_value", "Order value", "currency", (
    Variant("order_value", "Order booking value", "Sum of net value of non-cancelled orders booked by salesmen",
            _single("v", "sds_order", S("OrderNetValue"), "AttendanceId > 0"), pulse_kpi="OrdersValue"),
), glossary_term="Order Value"))

_add(Metric("orders", "Orders", "count", (
    Variant("order_count", "Number of orders", "Count of distinct non-cancelled orders booked in the period",
            _single("v", "sds_order", "uniqExact(SaleOrderId)", "SaleOrderId > 0")),
), glossary_term="Orders"))

_add(Metric("tc", "Total calls (TC)", "count", (
    Variant("visits", "Outlet visits", "Count of distinct outlet visits (calls) made by salesmen",
            _single("v", "sds_order", "uniqExact(AttendanceId)", "AttendanceId > 0"), pulse_kpi="TC"),
    Variant("day_summary", "Daily summary total", "Sum of total calls from each salesman's daily performance summary",
            _single("v", "edp", S("TotalTC"))),
), glossary_term="TC"))

_add(Metric("pc", "Productive calls (PC)", "count", (
    Variant("visits", "Productive outlet visits", "Count of distinct outlet visits that resulted in an order",
            _single("v", "sds_order", "uniqExact(AttendanceId)", "AttendanceId > 0 AND IsProductive = 1"), pulse_kpi="PC"),
    Variant("day_summary", "Daily summary total", "Sum of productive calls from each salesman's daily performance summary",
            _single("v", "edp", S("TotalPC"))),
), glossary_term="PC"))

_add(Metric("pc_pct", "Productive call %", "percent", (
    Variant("pc_by_tc", "PC / TC (visits)", "Productive calls / Total calls x 100, counted on outlet visits",
            {"pc": Component("sds_order", "uniqExact(AttendanceId)", "AttendanceId > 0 AND IsProductive = 1"),
             "tc": Component("sds_order", "uniqExact(AttendanceId)", "AttendanceId > 0")},
            ratio("pc", "tc")),
    Variant("day_summary", "PC / TC (daily summary)", "Sum of productive calls / Sum of total calls from salesmen's daily summaries x 100",
            {"pc": Component("edp", S("TotalPC")), "tc": Component("edp", S("TotalTC"))},
            ratio("pc", "tc"), pulse_kpi="ProductivityPct"),
    Variant("upc_by_utc", "Productive outlets / Visited outlets", "Distinct outlets that ordered / Distinct outlets visited x 100",
            {"upc": PRODUCTIVE, "utc": VISITED}, ratio("upc", "utc")),
    Variant("pc_by_sc", "PC / Scheduled calls", "Sum of productive calls / Sum of scheduled (planned) calls x 100",
            {"pc": Component("edp", S("TotalPC")), "sc": Component("edp", S("TotalSC"))},
            ratio("pc", "sc")),
), ambiguous=True, glossary_term="PC %"))

_add(Metric("visited_outlets", "Visited outlets (UTC)", "count", (
    Variant("visited", "Distinct outlets visited", "Count of distinct outlets visited at least once",
            {"v": VISITED}, pulse_kpi="VisitedOutlets"),
), glossary_term="UTC"))

_add(Metric("productive_outlets", "Productive outlets (UPC)", "count", (
    Variant("productive", "Distinct outlets that ordered", "Count of distinct outlets with at least one productive order",
            {"v": PRODUCTIVE}, pulse_kpi="ProductiveOutlets"),
), glossary_term="UPC"))

_add(Metric("coverage_pct", "Coverage %", "percent", (
    Variant("utc_by_assigned", "Visited / Assigned outlets", "Distinct outlets visited (UTC) / Distinct outlets on the assigned beat plan x 100",
            {"utc": VISITED, "assigned": ASSIGNED}, ratio("utc", "assigned"), pulse_kpi="OutletCoverage"),
    Variant("utc_by_mapped", "Visited / Mapped outlets", "Distinct outlets visited / All outlets on beats mapped to the salesmen x 100",
            {"utc": VISITED, "mapped": MAPPED}, ratio("utc", "mapped"), pulse_kpi="CoveragePercentageUTCbyMappedOutelts"),
    Variant("utc_by_universe", "Visited / Active outlet universe", "Distinct outlets visited / All active (unblocked) outlets in the outlet master x 100",
            {"utc": VISITED, "universe": UNIVERSE}, ratio("utc", "universe")),
    Variant("utc_by_scheduled", "Visited / Scheduled (daily)", "Sum of unique outlets visited per day / Sum of outlets scheduled per day x 100",
            {"utc": Component("edp", S("UTC")), "sc": Component("edp", S("TotalSC"))}, ratio("utc", "sc")),
), ambiguous=True, glossary_term="Coverage %"))

_add(Metric("penetration_pct", "Penetration %", "percent", (
    Variant("upc_by_assigned", "Productive / Assigned outlets", "Distinct outlets that ordered (UPC) / Distinct outlets on the assigned beat plan x 100",
            {"upc": PRODUCTIVE, "assigned": ASSIGNED}, ratio("upc", "assigned"), pulse_kpi="OutletPenetration"),
    Variant("upc_by_mapped", "Productive / Mapped outlets", "Distinct outlets that ordered / All outlets on beats mapped to the salesmen x 100",
            {"upc": PRODUCTIVE, "mapped": MAPPED}, ratio("upc", "mapped")),
    Variant("upc_by_universe", "Productive / Active outlet universe", "Distinct outlets that ordered / All active (unblocked) outlets in the outlet master x 100",
            {"upc": PRODUCTIVE, "universe": UNIVERSE}, ratio("upc", "universe")),
    Variant("upc_by_utc", "Productive / Visited outlets", "Distinct outlets that ordered / Distinct outlets visited x 100",
            {"upc": PRODUCTIVE, "utc": VISITED}, ratio("upc", "utc")),
), ambiguous=True, glossary_term="Penetration %"))

_add(Metric("ubo", "UBO (Unique billed outlets)", "count", (
    Variant("invoiced", "Outlets with an invoice", "Count of distinct outlets with at least one non-cancelled invoice in the period",
            {"v": BILLED}, pulse_kpi="BilledOutlets"),
    Variant("ordered", "Outlets with an order", "Count of distinct outlets with at least one productive (ordered) visit in the period",
            {"v": PRODUCTIVE}),
    Variant("invoiced_paid", "Outlets with a paid invoice line", "Count of distinct outlets with at least one invoice line of value above zero (ignores free-goods-only bills)",
            {"v": Component("sds_invoice", "uniqExact(OutletId)", "OutletId > 0 AND InvoiceNetValue > 0")}),
), ambiguous=True, glossary_term="UBO"))

_add(Metric("ubo_pct", "UBO %", "percent", (
    Variant("ubo_by_assigned", "Billed / Assigned outlets", "Distinct billed outlets (UBO) / Distinct outlets on the assigned beat plan x 100",
            {"ubo": BILLED, "assigned": ASSIGNED}, ratio("ubo", "assigned"), pulse_kpi="UBOPercentage"),
    Variant("ubo_by_mapped", "Billed / Mapped outlets", "Distinct billed outlets / All outlets on beats mapped to the salesmen x 100",
            {"ubo": BILLED, "mapped": MAPPED}, ratio("ubo", "mapped")),
    Variant("ubo_by_universe", "Billed / Active outlet universe", "Distinct billed outlets / All active (unblocked) outlets in the outlet master x 100",
            {"ubo": BILLED, "universe": UNIVERSE}, ratio("ubo", "universe")),
    Variant("ubo_by_utc", "Billed / Visited outlets", "Distinct billed outlets / Distinct outlets visited x 100",
            {"ubo": BILLED, "utc": VISITED}, ratio("ubo", "utc")),
), ambiguous=True, glossary_term="UBO %"))

_add(Metric("active_outlets", "Active outlets", "count", (
    Variant("unblocked", "Unblocked outlets in master", "Count of outlets not blocked in the outlet master (today's snapshot)",
            {"v": UNIVERSE}, pulse_kpi="ActiveOutletsNew"),
    Variant("on_beats", "Outlets on mapped beats", "Count of distinct outlets on beats mapped to salesmen in the period",
            {"v": MAPPED}, pulse_kpi="TotalActiveOutlets"),
    Variant("ordered", "Outlets that ordered", "Count of distinct outlets with at least one order in the period",
            {"v": PRODUCTIVE}),
    Variant("billed", "Outlets billed", "Count of distinct outlets with at least one invoice in the period",
            {"v": BILLED}),
), ambiguous=True, glossary_term="Active Outlets"))

_add(Metric("inactive_outlets", "Inactive outlets", "count", (
    Variant("blocked", "Blocked outlets in master", "Count of outlets marked blocked in the outlet master",
            {"v": Component("outlets", "count()", "IsBlocked = 1")}, pulse_kpi="InactiveOutlets"),
), higher_is_better=False, glossary_term="Inactive Outlets"))

_add(Metric("inactive_outlets_pct", "Inactive outlets %", "percent", (
    Variant("blocked_share", "Blocked / All outlets", "Outlets marked blocked / All outlets in the outlet master x 100",
            {"blocked": Component("outlets", "count()", "IsBlocked = 1"), "all": Component("outlets", "count()")},
            ratio("blocked", "all")),
), higher_is_better=False))

_add(Metric("new_outlets", "New outlets added", "count", (
    Variant("created", "Outlets created", "Count of outlets added to the outlet master in the period",
            {"v": Component("outlets_created", "count()")}),
), glossary_term="New Outlets"))

_add(Metric("avg_call_start_time", "Avg. call start time", "time", (
    Variant("first_call", "Average first call time", "Average of each salesman's first call time on working days, in company local time",
            {"v": Component("edp", "avgOrNull((toUnixTimestamp(FirstCallTime) + {tz_seconds}) % 86400)",
                            "FirstCallTime IS NOT NULL AND IsDayNotStarted = 0")}, pulse_kpi="AvgCallStartTime"),
), higher_is_better=False, glossary_term="Call Start Time"))

_add(Metric("avg_calls_per_rep", "Avg. calls per sales rep", "number", (
    Variant("tc_per_rep", "TC / Active reps", "Total calls / Number of salesmen who made at least one call",
            {"tc": Component("sds_order", "uniqExact(AttendanceId)", "AttendanceId > 0"),
             "reps": Component("sds_order", "uniqExact(FieldUserId)", "AttendanceId > 0")},
            ratio("tc", "reps", 1.0)),
), glossary_term="Avg Calls per Rep"))

_add(Metric("lpc", "Lines per call (LPC)", "number", (
    Variant("lines_per_call", "Order lines / Calls", "Order lines / Distinct calls (Pulse LPC)",
            {"lines": Component("sds_order", "toFloat64(count(ProductId))", "AttendanceId > 0"),
             "calls": Component("sds_order", "uniqExact(AttendanceId)", "AttendanceId > 0")},
            ratio("lines", "calls", 1.0), pulse_kpi="LPC"),
), glossary_term="LPC"))

_add(Metric("drop_size", "Drop size (avg bill value)", "currency", (
    Variant("value_per_invoice", "Net value per invoice", "Secondary net invoice value / Number of invoices",
            {"val": Component("sds_invoice", S("InvoiceNetValue")),
             "inv": Component("sds_invoice", "uniqExact(InvoiceId)")},
            ratio("val", "inv", 1.0), pulse_kpi="DropSize"),
), glossary_term="Drop Size"))


# ---------------------------------------------------------------------------
# Discipline and field-productivity metrics, from salesmen's daily performance rows
# (EmployeeDayPerformance: one row per rep per day). A "market day" is a day with at least one call.
# ---------------------------------------------------------------------------

_LOCAL = "((toUnixTimestamp({col}) + {{tz_seconds}}) % 86400)"
_MARKET_DAYS = "toFloat64(countIf(TotalTC > 0))"


def _edp(name: str, expr: str, where: str = "") -> dict[str, Component]:
    return {name: Component("edp", expr, where)}


_add(Metric("market_days", "Market days (rep-days)", "count", (
    Variant("market_days", "Rep-days with calls", "Count of rep-days with at least one call",
            _edp("v", _MARKET_DAYS)),
), glossary_term="Market Days"))

_add(Metric("working_reps", "Reps in market", "count", (
    Variant("reps_with_calls", "Reps with calls", "Count of distinct sales reps who made at least one call",
            _edp("v", "toFloat64(uniqExactIf(FieldUserId, TotalTC > 0))")),
), glossary_term="Active Reps"))

_add(Metric("calls_per_day", "Calls per rep per day", "number", (
    Variant("tc_per_market_day", "TC / Market days", "Total calls / Rep-days with calls (daily summaries)",
            {"tc": Component("edp", S("TotalTC")), "days": Component("edp", _MARKET_DAYS)},
            ratio("tc", "days", 1.0)),
), glossary_term="Calls per Day"))

_add(Metric("pc_per_day", "Productive calls per rep per day", "number", (
    Variant("pc_per_market_day", "PC / Market days", "Productive calls / Rep-days with calls (daily summaries)",
            {"pc": Component("edp", S("TotalPC")), "days": Component("edp", _MARKET_DAYS)},
            ratio("pc", "days", 1.0)),
)))

_add(Metric("zero_order_days", "Zero-order days", "count", (
    Variant("calls_no_orders", "Days with calls but no orders", "Count of rep-days with calls but no productive call",
            _edp("v", "toFloat64(countIf(TotalTC > 0 AND TotalPC = 0))")),
), higher_is_better=False, glossary_term="Zero Order Days"))

_add(Metric("leave_absent_days", "Leave / absent days", "count", (
    Variant("leave_or_absent", "Leave or absent", "Count of rep-days marked Leave or Absent",
            _edp("v", "toFloat64(countIf(IsAbsent = 1 OR toString(DayStartType) = 'Leave'))")),
), higher_is_better=False))

_add(Metric("holiday_leave_days", "Holiday and leave days", "count", (
    Variant("holiday_or_leave", "Holiday or leave", "Count of rep-days marked Holiday, Leave or Weekly off",
            _edp("v", "toFloat64(countIf(toString(DayStartType) IN ('Holiday', 'Leave', 'WeeklyOff')))")),
), higher_is_better=False))

_add(Metric("avg_last_call_time", "Avg. last call time", "time", (
    Variant("last_call", "Average last call time", "Average of each rep's last call time on market days, local time",
            _edp("v", "avgOrNull(" + _LOCAL.format(col="LastCallTime") + ")", "LastCallTime IS NOT NULL AND TotalTC > 0")),
)))

_add(Metric("avg_day_start_time", "Avg. day start time", "time", (
    Variant("day_start", "Average day start time", "Average time reps started their day in the app, local time",
            _edp("v", "avgOrNull(" + _LOCAL.format(col="DayStartTime") + ")", "IsDayNotStarted = 0 AND TotalTC > 0")),
)))

_add(Metric("time_in_market", "Avg. time in market (min)", "number", (
    Variant("retailing_minutes", "Retailing minutes per market day", "Average minutes spent retailing per rep-day with calls",
            _edp("v", "avgOrNull(TimeSpentRetailinginMinutes)", "TotalTC > 0")),
)))

_add(Metric("tour_plan_adherence_pct", "Tour plan adherence %", "percent", (
    Variant("adhered_days", "Days on plan / Market days", "Rep-days that followed the tour plan / Rep-days with calls x 100",
            {"ok": Component("edp", "toFloat64(countIf(IsTourPlanAdhered = 1))", "TotalTC > 0"),
             "days": Component("edp", _MARKET_DAYS)}, ratio("ok", "days")),
), glossary_term="PJP Adherence"))

# Metrics with a number in them ("first call after 11 AM", "days with at least 25 calls"). The catalog keeps
# them with a default filled in; the analyst asks for another value with with_param(metric_id, value)
# (seconds of the day, or a call count).
PARAM_METRICS: dict[str, tuple[Variant, float]] = {}


def _fill(variant: Variant, value: float) -> Variant:
    comps = {k: Component(c.source, c.expr.replace("@N@", str(int(round(value)))), c.where)
             for k, c in variant.components.items()}
    return Variant(variant.id, variant.label, variant.formula, comps, variant.combine, variant.pulse_kpi)


def _param(m: Metric, default: float) -> None:
    PARAM_METRICS[m.id] = (m.variants[0], default)
    _add(Metric(m.id, m.label, m.unit, (_fill(m.variants[0], default),), m.ambiguous, m.higher_is_better,
                m.glossary_term))


def with_param(metric_id: str, value: float | None) -> Variant:
    """The parameterised metric's formula with the number from the question (an integer literal in the SQL)."""
    template, default = PARAM_METRICS[metric_id]
    return _fill(template, default if value is None else value)


_param(Metric("late_start_days", "Late-start days", "count", (
    Variant("first_call_after", "Days with first call after a time", "Count of market days whose first call was after the given time",
            _edp("v", "toFloat64(countIf(" + _LOCAL.format(col="FirstCallTime") + " > @N@))",
                 "FirstCallTime IS NOT NULL AND TotalTC > 0")),
), higher_is_better=False), 10.5 * 3600)

_param(Metric("early_end_days", "Early-finish days", "count", (
    Variant("last_call_before", "Days with last call before a time", "Count of market days whose last call was before the given time",
            _edp("v", "toFloat64(countIf(" + _LOCAL.format(col="LastCallTime") + " < @N@))",
                 "LastCallTime IS NOT NULL AND TotalTC > 0")),
), higher_is_better=False), 16 * 3600)

_param(Metric("early_start_days", "Early-start days", "count", (
    Variant("day_start_before", "Days started before a time", "Count of market days started before the given time",
            _edp("v", "toFloat64(countIf(" + _LOCAL.format(col="DayStartTime") + " < @N@))",
                 "IsDayNotStarted = 0 AND TotalTC > 0")),
)), 9.5 * 3600)

_param(Metric("days_min_calls", "Days with enough calls", "count", (
    Variant("days_at_least_n_calls", "Days with at least N calls", "Count of rep-days with at least the given number of calls",
            _edp("v", "toFloat64(countIf(TotalTC >= @N@))")),
)), 25)

_param(Metric("low_call_days", "Low-call days", "count", (
    Variant("days_under_n_calls", "Days with fewer than N calls", "Count of market days with fewer than the given number of calls",
            _edp("v", "toFloat64(countIf(TotalTC > 0 AND TotalTC < @N@))")),
), higher_is_better=False), 10)


def metric_supports(metric_id: str, variant_id: str | None, dim: str | None) -> bool:
    return METRICS[metric_id].variant(variant_id).supports(dim)


def pick_variant(metric_id: str, preferred: str | None, dims: list[str]) -> tuple[Variant, str | None]:
    """The preferred variant if it supports the breakdown, else the first that does.

    Returns the variant and a note when Cooper had to switch formulas.
    """
    metric = METRICS[metric_id]
    wanted = metric.variant(preferred)
    if all(wanted.supports(d) for d in dims):
        return wanted, None
    for v in metric.variants:
        if all(v.supports(d) for d in dims):
            label = ", ".join(DIM_LABELS.get(d, d) for d in dims)
            return v, (f"The formula \"{wanted.label}\" can't be split by {label}, "
                       f"so I used \"{v.label}\" for this breakdown.")
    return wanted, None


def catalog_summary() -> str:
    """Compact text description of the catalog for the LLM prompt."""
    lines = []
    for m in METRICS.values():
        lines.append(f"- {m.id}: {m.label}. Basic formula: {m.default.formula}")
    return "\n".join(lines)
