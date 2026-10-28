"""Offline checks: every test-sheet question parses, builds guarded SQL and gets an answer."""

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app import config, glossary, pipeline  # noqa: E402
from backend.app.interpreter import parse  # noqa: E402
from scripts.eval_98 import FakeMCP  # noqa: E402

QUESTIONS = json.loads((ROOT / "backend/tests/fixtures/test_sheet_98.json").read_text(encoding="utf-8"))
TODAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    monkeypatch.setattr(config, "PREFS_DIR", tmp_path)


@pytest.mark.parametrize("q", QUESTIONS, ids=[str(q["id"]) for q in QUESTIONS])
def test_every_question_parses(q):
    plan = parse(q["query"], TODAY)
    assert plan.intent in ("data", "glossary", "help")
    if plan.intent == "data":
        assert plan.metrics or plan.placeholder_dim, q["query"]


def _run(question):
    async def go():
        events = [ev async for ev in pipeline.run(question, pipeline.CompanyCtx(10600, "Deoleo"), mcp=FakeMCP(),
                                                  use_llm_summary=False)]
        return events
    return asyncio.run(go())


@pytest.mark.parametrize("q", QUESTIONS, ids=[str(q["id"]) for q in QUESTIONS])
def test_every_question_answers(q):
    events = _run(q["query"])
    answer = events[-1]["answer"]
    assert answer["kind"] in ("data", "glossary", "clarify", "help"), answer["summary"]
    assert answer["timings"]["total"] < 60


def test_ambiguous_metric_offers_four_formulas():
    answer = _run("Show UBO for last month")[-1]["answer"]
    note = answer["formula_notes"][0]
    assert note["metric"] == "ubo"
    assert "basic formula" in note["message"]
    assert 2 <= len(note["options"]) <= 4


def test_other_company_refused_before_any_query():
    mcp = FakeMCP()

    async def go():
        return [ev async for ev in pipeline.run("show sales of company 10601", pipeline.CompanyCtx(10600, "Deoleo"),
                                                mcp=mcp, use_llm_summary=False)]
    events = asyncio.run(go())
    assert events[-1]["answer"]["kind"] == "refusal"
    assert mcp.log == []


def test_glossary_slang():
    found = {t.term for t in glossary.find_terms("whats the ECO and drop size for my beat")}
    assert {"Drop size"} <= found or "Drop Size" in found
    assert len(glossary.terms()) >= 60
