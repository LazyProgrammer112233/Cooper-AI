"""FMCG / CPG glossary: full forms, field slang and basic formulas.

Every question is run through this before interpretation, so "strike rate",
"bill cut outlets" or "sell-in" land on the right metric, and answers can show
the full form of every abbreviation they use.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from .config import DATA_DIR


@dataclass(frozen=True)
class Term:
    term: str
    full_form: str
    aliases: tuple[str, ...]
    category: str
    definition: str
    basic_formula: str | None
    metric_id: str | None


@lru_cache(maxsize=1)
def terms() -> tuple[Term, ...]:
    raw = json.loads((DATA_DIR / "fmcg_glossary.json").read_text(encoding="utf-8"))
    out = []
    for t in raw["terms"]:
        out.append(Term(
            term=t["term"], full_form=t["full_form"], aliases=tuple(a.lower() for a in t.get("aliases", [])),
            category=t["category"], definition=t["definition"], basic_formula=t.get("basic_formula"),
            metric_id=t.get("metric_id"),
        ))
    return tuple(out)


def _pattern(alias: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


@lru_cache(maxsize=1)
def _alias_index() -> list[tuple[str, re.Pattern, Term]]:
    rows = []
    for t in terms():
        names = set(t.aliases) | {t.term.lower()}
        for a in names:
            if len(a) < 2:
                continue
            rows.append((a, _pattern(a), t))
    rows.sort(key=lambda r: -len(r[0]))
    return rows


def find_terms(text: str) -> list[Term]:
    """Glossary terms mentioned in the text (longest aliases first, no overlaps)."""
    low = text.lower()
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, Term]] = []
    for alias, pat, term in _alias_index():
        for m in pat.finditer(low):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            if term not in [f[1] for f in found]:
                found.append((m.start(), term))
    found.sort(key=lambda f: f[0])
    return [f[1] for f in found]


def lookup(name: str) -> Term | None:
    low = name.strip().lower()
    for t in terms():
        if low == t.term.lower() or low in t.aliases or low == t.full_form.lower():
            return t
    hits = find_terms(low)
    return hits[0] if hits else None


def by_metric(metric_id: str) -> Term | None:
    for t in terms():
        if t.metric_id == metric_id:
            return t
    return None


def prompt_block(max_terms: int = 80) -> str:
    """Compact glossary for the LLM prompt."""
    lines = []
    for t in terms()[:max_terms]:
        alias = ", ".join(t.aliases[:5])
        f = f" Formula: {t.basic_formula}." if t.basic_formula else ""
        lines.append(f"- {t.term} = {t.full_form} (also: {alias}). {t.definition}{f}")
    return "\n".join(lines)


def abbreviations_in(text: str) -> list[dict]:
    """Full forms for the abbreviations shown in an answer."""
    out = []
    seen = set()
    for t in terms():
        if t.term.lower() == t.full_form.lower() or t.term in seen:
            continue
        candidates = [t.term] + [a.upper() for a in t.aliases if a.isalpha() and 2 <= len(a) <= 5 and a.upper() == a.upper()]
        for c in candidates:
            if c.isupper() and re.search(r"(?<![A-Za-z])" + re.escape(c) + r"(?![A-Za-z])", text):
                out.append({"term": c, "full_form": t.full_form})
                seen.add(t.term)
                break
    return out
