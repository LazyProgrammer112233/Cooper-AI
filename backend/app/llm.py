"""Local LLM through Ollama (http://localhost:11434). No API keys, nothing leaves the laptop.

The LLM has two small jobs:
1. interpret: when the rule parser can't find a metric, map the question to catalog ids (JSON only);
2. summarise: turn result numbers into 2 to 3 plain sentences (numbers are checked afterwards).
It never sees SQL, table names, credentials or any other company's data.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date

import httpx

from . import config, glossary
from .catalog import ALL_DIMS, METRICS, catalog_summary

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {"type": "array", "items": {"type": "string", "enum": list(METRICS)}, "maxItems": 3},
        "shape": {"type": "string", "enum": ["value", "trend", "breakdown", "ranking"]},
        "dims": {"type": "array", "items": {"type": "string", "enum": list(ALL_DIMS) + ["day", "week", "month"]}, "maxItems": 2},
        "order": {"type": "string", "enum": ["asc", "desc"]},
        "limit": {"type": "integer"},
        "period": {"type": "string", "enum": ["today", "yesterday", "this_week", "last_week", "mtd", "last_month",
                                               "qtd", "last_quarter", "ytd", "custom", "none"]},
        "start_date": {"type": "string", "description": "YYYY-MM-DD, only when period is custom"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD, only when period is custom"},
        "filters": {"type": "array", "items": {"type": "object", "properties": {
            "dim": {"type": "string"}, "value": {"type": "string"}}, "required": ["value"]}},
        "understood": {"type": "boolean"},
    },
    "required": ["metrics", "shape", "understood"],
}


def _system_prompt() -> str:
    today = date.today().isoformat()
    return (
        "You are the question interpreter for Cooper AI, a sales analytics assistant for one Indian FMCG company.\n"
        "Map the user's question to the metric catalog below. Reply with JSON only.\n"
        "Rules: use only metric ids from the catalog; never invent metrics; if the question is not about these "
        "sales metrics set understood=false and metrics=[]. 'Sales' without primary/secondary means secondary_sales.\n"
        "Always interpret Indian FMCG abbreviations and slang using the glossary.\n"
        "For a specific date or date range, set period=custom with start_date and end_date (YYYY-MM-DD). "
        f"Dates are day-first (23/10 is 23 October). Today is {today}.\n\n"
        f"METRIC CATALOG:\n{catalog_summary()}\n\nGLOSSARY:\n{glossary.prompt_block(60)}\n"
    )


class LocalLLM:
    def __init__(self, url: str | None = None, model: str | None = None):
        self.url = (url or config.OLLAMA_URL).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.enabled = config.LLM_ENABLED

    async def health(self) -> dict:
        if not self.enabled:
            return {"ok": False, "reason": "disabled"}
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self.url}/api/tags")
                r.raise_for_status()
                names = [m.get("name", "") for m in r.json().get("models", [])]
                return {"ok": any(n.startswith(self.model.split(":")[0]) for n in names), "model": self.model, "installed": names}
        except Exception as exc:  # Ollama not running
            return {"ok": False, "reason": str(exc)[:200], "model": self.model}

    async def _chat(self, messages: list[dict], timeout: float, fmt=None, max_tokens: int = 300) -> str:
        body = {
            "model": self.model, "messages": messages, "stream": False, "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        if fmt is not None:
            body["format"] = fmt
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await asyncio.wait_for(client.post(f"{self.url}/api/chat", json=body), timeout=timeout)
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "")

    async def interpret(self, question: str, timeout: float) -> dict | None:
        if not self.enabled:
            return None
        try:
            text = await self._chat(
                [{"role": "system", "content": _system_prompt()}, {"role": "user", "content": question}],
                timeout=timeout, fmt=PLAN_SCHEMA, max_tokens=200,
            )
            data = json.loads(text)
            if not data.get("understood"):
                return None
            return data
        except Exception:
            return None

    async def summarise(self, question: str, facts: str, allowed_numbers: set[str], timeout: float) -> str | None:
        """2 to 3 sentences. Rejected if it contains a number that is not in the facts."""
        if not self.enabled:
            return None
        prompt = (
            "Write a short, friendly answer (2 to 3 sentences, no lists, no markdown) to the question, using ONLY "
            "the facts given. Copy numbers exactly as written in the facts. Do not add any other numbers, "
            "advice or guesses. Mention the period.\n\n"
            f"QUESTION: {question}\n\nFACTS:\n{facts}"
        )
        try:
            text = await self._chat([{"role": "user", "content": prompt}], timeout=timeout, max_tokens=180)
        except Exception:
            return None
        text = text.strip().strip('"')
        if not text or len(text) > 700:
            return None
        if not numbers_are_grounded(text, allowed_numbers):
            return None
        return text


_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)")


def numbers_are_grounded(text: str, allowed: set[str]) -> bool:
    allowed_norm = {a.replace(",", "") for a in allowed}
    for m in _NUM.finditer(text):
        raw = m.group(1).replace(",", "")
        if raw in allowed_norm:
            continue
        return False
    return True


def numbers_in(text: str) -> set[str]:
    return {m.group(1).replace(",", "") for m in _NUM.finditer(text)}
