"""Tenant isolation guardrails.

Layer 1  Session lock      - the company id lives in a signed token, set once per chat.
Layer 2  Question gate     - questions about other companies or prompt tricks never reach data.
Layer 3  SQL guard         - every statement is parsed; each table must be an allowed table that
                             sits alone in its SELECT, filtered by `<company column> = <locked id>`.
Layer 4  Result check      - done in pipeline.py: rows must only carry this company's entities.

Nothing here trusts the LLM. The SQL guard runs on SQL the backend itself built, as a second
line of defence against bugs in the builder.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

import sqlglot
from sqlglot import exp

from . import config

# table -> company column. Anything not listed here cannot be queried.
ALLOWED_TABLES: dict[str, str] = {
    "SecondaryDemandSale": "CompanyId",
    "PrimaryReprimary": "CompanyId",
    "EmployeeJourneyDetail": "CompanyId",
    "EmployeeDayPerformance": "CompanyId",
    "master_f2klocations": "Company",
    "master_facompanyzones": "Company",
    "master_regions": "CompanyId",
    "master_territories": "CompanyId",
    "master_faproductprimarycategory": "CompanyId",
    "master_faproductsecondarycategory": "CompanyId",
    "master_faproductdivision": "CompanyId",
    "master_facompanyproducts": "CompanyId",
    "master_fadistributors": "CompanyId",
    "master_clientemployees": "Company",
    "master_companies": "Id",
    "client_timezones": "client_id",
    "client_fiscal_config": "client_id",
}


class GuardViolation(Exception):
    pass


# ---------------------------------------------------------------------------
# Layer 1: signed company lock
# ---------------------------------------------------------------------------


def sign_company(company_id: int, company_name: str, tz: int = 330, fy: int = 4) -> str:
    claims = {"cid": int(company_id), "name": company_name, "tz": int(tz), "fy": int(fy), "iat": int(time.time())}
    body = json.dumps(claims, separators=(",", ":"))
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    sig = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_company_token(token: str) -> dict:
    try:
        payload, sig = token.rsplit(".", 1)
    except (ValueError, AttributeError) as exc:
        raise GuardViolation("Missing or malformed company session") from exc
    expected = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise GuardViolation("Company session signature is invalid")
    padded = payload + "=" * (-len(payload) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    if not isinstance(data.get("cid"), int) or data["cid"] <= 0:
        raise GuardViolation("Company session has no company")
    return data


# ---------------------------------------------------------------------------
# Layer 2: question gate
# ---------------------------------------------------------------------------

_INJECTION = re.compile(
    r"ignore (all |your |the |previous |prior )*(instructions|rules|guardrails)|system prompt|"
    r"you are now|act as|jailbreak|developer mode|reveal (your|the) (prompt|instructions)|"
    r"\b(drop|delete|truncate|insert|update|alter|grant)\s+(table|from|into|database)\b|;\s*select|--|/\*|"
    r"\b(bh?ool|bhul) ja(o|ao|iye)\b|\bbhula do\b|\bignore kar(o|do|iye)\b|\bniyam tod|"
    r"\b(instructions?|nirdesh|rules?) (bhool|bhul|ignore|hata)|भूल जाओ|निर्देश",
    re.I,
)
_OTHER_COMPANY = re.compile(
    r"\b(other|another|different|competitor'?s?|rival)\s+(companies|company|clients?|customers? companies|tenants?)\b|"
    r"\b(all|every|each)\s+(the\s+)?(companies|company|tenants?)\b|"
    r"\bcompany\s*(id)?\s*[:#=]?\s*\d{3,}\b|\bclient\s*id\b|\btenant\b|\blist (of )?(all )?(companies|clients)\b|"
    r"\bswitch (to )?(company|client)\b|\bcompare (us |our \w+ )?with (company|client)\b|"
    r"\b(dusri|doosri|dusre|doosre|anya|baaki|sabhi|saari|sab) (company|companies|kampani|client)\b|"
    r"दूसरी कंपनी|अन्य कंपनी|सभी कंपनियों",
    re.I,
)


# Other FMCG companies and their best-known brands. A question naming one is about someone else's data
# (or a trick), unless the name belongs to the locked company itself.
_OTHER_BRANDS: dict[str, str] = {
    r"mars": "mars", r"nestl[eé]": "nestle", r"maggi(e|es)?": "nestle", r"kit ?kat": "nestle", r"nescafe": "nestle",
    r"hul": "hindustan unilever", r"hindustan unilever": "hindustan unilever", r"unilever": "unilever",
    r"lifebuoy": "hindustan unilever", r"surf excel": "hindustan unilever", r"itc": "itc", r"aashirvaad": "itc",
    r"colgate( palmolive)?": "colgate", r"colpal": "colgate", r"haldiram'?s?": "haldiram",
    r"coca[ -]?cola": "coca cola", r"coke": "coca cola", r"pepsi(co)?": "pepsico", r"kurkure": "pepsico",
    r"dabur": "dabur", r"britannia": "britannia", r"parle": "parle", r"amul": "amul", r"marico": "marico",
    r"godrej": "godrej", r"emami": "emami", r"patanjali": "patanjali", r"p ?& ?g": "procter", r"procter": "procter",
    r"tata consumer": "tata", r"mondelez": "mondelez", r"cadbury": "mondelez", r"oreo": "mondelez",
    r"ferrero": "ferrero", r"kellogg'?s?": "kellogg", r"perfetti": "perfetti", r"reckitt": "reckitt",
    r"dettol": "reckitt", r"horlicks": "horlicks", r"bournvita": "mondelez", r"kandhari": "kandhari", r"kbpl": "kandhari",
    r"bisleri": "bisleri", r"jyothy": "jyothy",
}
_OTHER_BRANDS_RE = [(re.compile(rf"(?<![a-z0-9]){p}(?![a-z0-9])", re.I), owner) for p, owner in _OTHER_BRANDS.items()]

# General-knowledge and chit-chat topics Cooper must not answer.
_OFF_TOPIC = re.compile(
    r"\b(prime minister|president|chief minister|governor|parliament|elections?|politic\w*|minister|"
    r"capital (city )?of|weather|temperature|rain(fall)?|cricket|ipl|football|fifa|olympics?|"
    r"movies?|films?|actors?|actress|songs?|music|singer|celebrit\w+|bollywood|hollywood|recipes?|cooking|"
    r"jokes?|poems?|poetry|horoscope|astrology|zodiac|religion|god|bitcoin|crypto\w*|stock market|share price|"
    r"sensex|nifty|news|translate|meaning of life|girlfriend|boyfriend|chatgpt|openai|gemini|"
    r"write (a|an|me|some) (code|program|script|essay|letter|email|story|poem|song|article)|"
    r"(python|javascript|java|c\+\+) (code|program|function)|who (invented|discovered|founded|wrote)|"
    r"(height|age|wife|husband) of|universe|planet|galaxy|solar system|moon|sun\b)\b",
    re.I,
)


def other_brand_in(question: str, company_name: str = "") -> str | None:
    """The other company or brand the question names, unless it is the locked company's own."""
    own = company_name.lower()
    for rx, owner in _OTHER_BRANDS_RE:
        m = rx.search(question)
        if m and owner not in own and m.group(0).lower() not in own:
            return m.group(0)
    return None


def gate_question(question: str, company_id: int, other_company_names: set[str] | None = None,
                  company_name: str = "") -> str | None:
    """Return a refusal reason, or None when the question may proceed."""
    q = question.strip()
    if not q:
        return "empty"
    if len(q) > 600:
        return "too_long"
    if _INJECTION.search(q):
        return "injection"
    if other_brand_in(q, company_name):
        return "other_brand"
    if _OFF_TOPIC.search(q):
        return "off_topic"
    m = _OTHER_COMPANY.search(q)
    if m:
        digits = re.findall(r"\d{3,}", m.group(0))
        if not digits or any(int(d) != int(company_id) for d in digits):
            return "other_company"
    if other_company_names:
        low = f" {re.sub(r'[^a-z0-9 ]', ' ', q.lower())} "
        for name in other_company_names:
            if f" {name} " in low:
                return "other_company"
    return None


REFUSALS = {
    "other_company": ("I can only answer questions about {company}, the company selected for this chat. "
                      "I can't share or compare data from any other company. "
                      "If you need another company, start a new chat and select it there."),
    "injection": ("I can't do that. I only answer sales and distribution questions about {company}, "
                  "using its own data."),
    "other_brand": ("That's against my policy. \"{name}\" isn't part of {company}'s data, and I can't answer about "
                    "other companies or their brands. I only answer FMCG analytics questions about {company}'s own "
                    "sales, calls, coverage, outlets, distributors, products and sales reps."),
    "off_topic": ("That's outside what I can help with, and against my policy to answer. I'm an FMCG analytics "
                  "assistant: I only answer questions about {company}'s own data, such as sales, calls, coverage, "
                  "outlets, distributors, products and sales reps."),
    "too_long": "That question is a bit long for me. Could you ask it in one or two sentences?",
    "empty": "Please type a question about {company}'s sales, calls, coverage or outlets.",
}


# ---------------------------------------------------------------------------
# Layer 3: SQL guard
# ---------------------------------------------------------------------------


def _company_literal(cond: exp.Expression, col: str, aliases: set[str]) -> list[int]:
    """Company ids that `cond` pins `col` to with a top-level AND of `col = <int>`."""
    found: list[int] = []
    for eq in _top_level_conjuncts(cond):
        if not isinstance(eq, exp.EQ):
            continue
        left, right = eq.left, eq.right
        if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            left, right = right, left
        if (isinstance(left, exp.Column) and left.name == col and isinstance(right, exp.Literal)
                and not right.is_string and (not left.table or left.table in aliases)):
            try:
                found.append(int(right.this))
            except ValueError:
                continue
    return found


def _top_level_conjuncts(cond: exp.Expression):
    if isinstance(cond, exp.Paren):
        yield from _top_level_conjuncts(cond.this)
    elif isinstance(cond, exp.And):
        yield from _top_level_conjuncts(cond.left)
        yield from _top_level_conjuncts(cond.right)
    else:
        yield cond


def check_sql(sql: str, company_id: int) -> None:
    """Raise GuardViolation unless every table read is pinned to `company_id`."""
    cid = int(company_id)
    try:
        statements = sqlglot.parse(sql, read="clickhouse")
    except sqlglot.errors.ParseError as exc:
        raise GuardViolation(f"SQL could not be parsed: {exc}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise GuardViolation("Exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise GuardViolation("Only SELECT statements are allowed")
    for bad in (exp.Insert, exp.Delete, exp.Update, exp.Drop, exp.Create, exp.Alter, exp.Command):
        if tree.find(bad):
            raise GuardViolation(f"{bad.__name__} is not allowed")
    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower() in {"remote", "remotesecure", "url", "file", "s3", "mysql", "postgresql", "cluster", "input"}:
            raise GuardViolation(f"Table function {fn.name} is not allowed")

    tables = list(tree.find_all(exp.Table))
    if not tables:
        raise GuardViolation("Query reads no table")
    for table in tables:
        name = table.name
        if name not in ALLOWED_TABLES:
            raise GuardViolation(f"Table {name!r} is not allowed")
        if table.db and table.db != config.CH_DATABASE:
            raise GuardViolation(f"Database {table.db!r} is not allowed")
        select = table.find_ancestor(exp.Select)
        if select is None:
            raise GuardViolation(f"Table {name} is not inside a SELECT")
        from_ = select.args.get("from")
        source = from_.this if from_ is not None else None
        if isinstance(source, exp.Final):
            source = source.this
        if source is not table:
            raise GuardViolation(f"Table {name} must be the only source of its SELECT")
        if select.args.get("joins"):
            for j in select.args["joins"]:
                joined = j.this.this if isinstance(j.this, exp.Final) else j.this
                if isinstance(joined, exp.Table):
                    raise GuardViolation(f"Table {joined.name} must be wrapped in its own filtered SELECT")
        where = select.args.get("where")
        prewhere = select.args.get("prewhere")
        aliases = {name}
        if table.alias:
            aliases.add(table.alias)
        col = ALLOWED_TABLES[name]
        pinned = []
        for clause in (where, prewhere):
            if clause is not None:
                pinned += _company_literal(clause.this, col, aliases)
        if not pinned:
            raise GuardViolation(f"Table {name} is not filtered by {col} = {cid}")
        if any(p != cid for p in pinned):
            raise GuardViolation("Query references another company id")

    # Any other comparison of a company column with a different literal is suspicious.
    company_cols = set(ALLOWED_TABLES.values()) - {"Id"}
    for cmp in tree.find_all(exp.EQ, exp.NEQ, exp.In, exp.GT, exp.LT, exp.GTE, exp.LTE):
        cols = [c for c in cmp.find_all(exp.Column) if c.name in company_cols]
        if not cols:
            continue
        for lit in cmp.find_all(exp.Literal):
            if not lit.is_string and lit.this.isdigit() and int(lit.this) != cid and int(lit.this) > 999:
                raise GuardViolation("Query compares a company column with another id")


def check_rows(rows: list[dict], company_id: int) -> None:
    """Layer 4: if a result carries a company column, it must be this company's."""
    for row in rows:
        for key in ("CompanyId", "Company", "company_id"):
            if key in row and row[key] not in (None, int(company_id), str(company_id)):
                raise GuardViolation("Result contains another company's rows")
