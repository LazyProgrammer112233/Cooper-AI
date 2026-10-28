"""Offline checks for the 100 harder questions: master-data lists, combined filters with any date range,
and the anti-hallucination guard (off-topic, other companies, names that don't exist)."""

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app import config, pipeline  # noqa: E402
from backend.app.guard import check_sql  # noqa: E402
from backend.app.interpreter import parse  # noqa: E402
from backend.app.sqlbuild import LISTABLE, Filter, build_list  # noqa: E402
from scripts.eval_98 import FakeMCP  # noqa: E402

QUESTIONS = json.loads((ROOT / "backend/tests/fixtures/test_sheet_complex.json").read_text(encoding="utf-8"))
TODAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    monkeypatch.setattr(config, "PREFS_DIR", tmp_path)
    pipeline._CACHE.clear()


def _run(question):
    mcp = FakeMCP()

    async def go():
        return [ev async for ev in pipeline.run(question, pipeline.CompanyCtx(10600, "Deoleo"), mcp=mcp,
                                                use_llm_summary=False)]
    events = asyncio.run(go())
    return events[-1]["answer"], mcp.log


@pytest.mark.parametrize("q", QUESTIONS, ids=[str(q["id"]) for q in QUESTIONS])
def test_complex_question(q):
    exp = q["expect"]
    answer, log = _run(q["query"])
    assert answer["kind"] == exp["kind"], (q["query"], answer["summary"])
    if exp.get("no_query"):
        assert log == [], "refused questions must never reach Pulse"
    for sql in log:
        check_sql(sql, 10600)
    if "intent" not in exp:
        return
    plan = parse(pipeline._strip_company(q["query"], "Deoleo"), TODAY)
    assert plan.intent == exp["intent"]
    for key in ("list_dim", "count_only", "metrics", "shape", "dims"):
        if key in exp:
            assert getattr(plan, key) == exp[key], key
    if "period" in exp:
        assert [plan.period.start.isoformat(), plan.period.end.isoformat()] == exp["period"]
    if "filters" in exp:
        assert [[f.text, f.dim_hint] for f in plan.filters + plan.members] == exp["filters"]


@pytest.mark.parametrize("dim", LISTABLE)
def test_every_list_query_passes_the_guard(dim):
    for filters in ([], [Filter("zone", ids=[1])], [Filter("brand", ids=[2])], [Filter("state", names=["Delhi"])]):
        sql, _, _ = build_list(10600, dim, filters)
        check_sql(sql, 10600)


def test_own_brand_is_not_refused():
    from backend.app.guard import gate_question
    assert gate_question("Show Maggi sales for this month", 1, company_name="Nestle India") is None
    assert gate_question("Show Maggi sales for this month", 1, company_name="Deoleo") == "other_brand"


def test_list_answer_has_table_and_chart():
    answer, _ = _run("What are the product categories for Deoleo?")
    assert answer["table"]["rows"] and answer["chart"]["type"] in ("pie", "hbar", "table")
    assert "Deoleo has" in answer["summary"]
