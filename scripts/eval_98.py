"""Runs the 98 test-sheet questions through the full Cooper pipeline.

    python scripts/eval_98.py --company 10600 --name Deoleo          # live, needs PULSE_MCP_URL/TOKEN in .env
    python scripts/eval_98.py --dry                                   # offline, synthetic rows (checks plumbing + guardrails)

Writes a report to scripts/out/eval_98.md and prints a one-line result per question.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import pipeline  # noqa: E402
from backend.app.mcp_client import PulseMCP, QueryResult  # noqa: E402

FIXTURE = ROOT / "backend" / "tests" / "fixtures" / "test_sheet_98.json"


# Names the fake company does not have, so "not found" answers can be tested offline.
UNKNOWN_NAMES = {"earth", "jupiter", "atlantis", "gotham", "narnia", "xyzzy", "prime minister"}


class _FakeSession:
    """Answers with plausible rows so the pipeline can be checked without Pulse access."""

    def __init__(self, log: list[str]):
        self.log = log
        self.rng = random.Random(7)

    async def ch_query(self, sql: str, top: int = 500, timeout: float = 20) -> QueryResult:
        self.log.append(sql)
        rows: list[dict] = []
        if " AS dim," in sql:
            first_dim = re.search(r"SELECT '(\w+)' AS dim", sql).group(1)
            terms = re.search(r"IN \(([^)]*)\)", sql).group(1)
            for t in re.findall(r"'((?:[^'\\]|\\.)*)'", terms):
                if t in ("xyz", "abc", "nowhere") or t in UNKNOWN_NAMES:
                    continue
                rows.append({"dim": first_dim, "id": 100 + len(rows), "name": t.title(), "term": t})
        elif re.match(r"SELECT (i\.id|s\.sku|ifNull\(c\.cat_name|\* FROM|initcapUTF8)", sql):  # master lists
            for i, n in enumerate(["Pure", "EV", "EL", "Lotion"]):
                rows.append({"id": 500 + i, "name": n, "n": 10 * (i + 1), "category": "Pure", "brand": "Bertolli",
                             "brands": "Bertolli, Figaro", "mrp": 99.0 + i, "city": "Pune", "state": "Maharashtra", "_k": n})
        elif sql.startswith("SELECT any(") or "AS name, count() AS n" in sql:
            rows = [{"name": n, "id": i} for i, n in enumerate(["East", "North", "South", "West"])]
        else:
            nkeys = len(set(re.findall(r"\bAS k(\d)\b", sql)))
            n = 1 if nkeys == 0 else 6
            for i in range(n):
                r: dict = {"v": round(self.rng.uniform(10, 1e6), 2)}
                for k in range(nkeys):
                    if "toString(intDiv(s._dk, 100))" in sql and k == 0:
                        r[f"k{k}"] = str(202605 + i)
                    elif "toString(makeDate" in sql or "toString(toMonday" in sql:
                        r[f"k{k}"] = (date(2026, 10, 1) + timedelta(days=i)).isoformat()
                    else:
                        r[f"k{k}"] = 1000 + i
                        r[f"k{k}_name"] = f"Item {i + 1}"
                rows.append(r)
        return QueryResult(columns=list(rows[0]) if rows else [], rows=rows, elapsed=0.01, sql=sql)


class FakeMCP:
    def __init__(self):
        self.log: list[str] = []

    @asynccontextmanager
    async def session(self):
        yield _FakeSession(self.log)


async def run_one(q: str, ctx, mcp, use_llm: bool) -> dict:
    events = []
    t = time.monotonic()
    async for ev in pipeline.run(q, ctx, mcp=mcp, use_llm_summary=use_llm):
        events.append(ev)
    ans = events[-1]["answer"]
    ans["_elapsed"] = round(time.monotonic() - t, 2)
    return ans


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", type=int, default=10600)
    ap.add_argument("--name", default="Deoleo")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    if args.dry or args.no_llm:
        pipeline.config.LLM_ENABLED = False
    questions = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        questions = [q for q in questions if q["id"] in wanted]
    ctx = pipeline.CompanyCtx(id=args.company, name=args.name)
    mcp = FakeMCP() if args.dry else PulseMCP()

    out_dir = ROOT / "scripts" / "out"
    out_dir.mkdir(exist_ok=True)
    lines = [f"# Cooper AI: 98-question run for {args.name} ({args.company})", ""]
    kinds: dict[str, int] = {}
    slow = 0
    for q in questions:
        try:
            ans = await run_one(q["query"], ctx, mcp, not args.no_llm)
        except Exception as exc:  # report and continue
            ans = {"kind": "crash", "summary": f"{type(exc).__name__}: {exc}", "_elapsed": 0}
        kinds[ans["kind"]] = kinds.get(ans["kind"], 0) + 1
        slow += ans["_elapsed"] > 60
        chart = (ans.get("chart") or {}).get("type", "-")
        print(f"{q['id']:>3} {ans['kind']:<8} {chart:<5} {ans['_elapsed']:>5}s  {q['query'][:60]}")
        print(f"      {ans['summary'][:160]}")
        lines += [f"## {q['id']}. {q['query']}", "", f"**{ans['kind']}** · chart: {chart} · {ans['_elapsed']}s", "",
                  ans["summary"], ""]
        for n in ans.get("notes", []):
            lines.append(f"- Note: {n}")
        for f in ans.get("formula_notes", []):
            lines.append(f"- Formula: {f['message']}")
        lines.append("")
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items()))
    print(f"\n{summary}; over 60s: {slow}")
    lines.insert(2, f"Result: {summary}; answers over 60s: {slow}\n")
    (out_dir / ("eval_98_dry.md" if args.dry else "eval_98.md")).write_text("\n".join(lines), encoding="utf-8")
    return 1 if kinds.get("crash") else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
