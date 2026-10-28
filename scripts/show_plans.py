"""Print the parsed plan for every query in the test sheet (no data access)."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.interpreter import parse  # noqa: E402

rows = json.loads((Path(__file__).resolve().parents[1] / "backend/tests/fixtures/test_sheet_98.json").read_text())
for r in rows:
    p = parse(r["query"], date(2026, 10, 23))
    f = ",".join(f"{x.text}|{x.dim_hint}" for x in p.filters)
    m = ",".join(f"{x.text}|{x.dim_hint}" for x in p.members)
    print(f"{r['id']:>3} {r['query'][:60]:<60} -> {p.intent} {p.metrics} {p.shape} dims={p.dims} grain={p.grain} "
          f"per={p.period.preset if p.period else None} ord={p.order} lim={p.limit} ext={p.extreme} "
          f"f=[{f}] m=[{m}] ph={p.placeholder_dim}")
