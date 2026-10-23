# Cooper AI

Cooper is a sales-analytics chatbot for FieldAssist companies. It answers questions such as
"Which zone has the lowest coverage?" or "Show TC vs PC trend" from Pulse data, picks a table,
bar or pie chart by itself, and answers within a minute.

This is the first prototype. It runs on one laptop (localhost), uses a free local model through
Ollama, and reads data only through the **Pulse MCP**.

## What makes it different from the old Pulse AI chatbot

- **Numbers never come from the model.** Cooper turns the question into a plan, builds SQL from a
  fixed metric catalog, runs it through Pulse MCP and writes the answer from the result. The local
  model only helps understand unusual wording and rephrase the answer, and any number it writes
  that is not in the data is thrown away.
- **One company per chat, strictly.** See [Guardrails](#guardrails).
- **Indian FMCG vocabulary built in.** A glossary of 76 terms, short forms and slang (UBO, ECO,
  TC/PC, PJP, drop size, lakh/crore, DB/SS, MTD/LMTD...) is checked on every question.
- **Formulas are explicit.** Every metric uses a basic formula. Where companies differ (UBO,
  coverage, penetration, PC %, active outlets, primary and secondary sales) Cooper says which
  formula it used and offers up to four alternatives. A choice is saved for that company and used
  for all later answers.

## Quick start

You need Python 3.10+, Node 18+, [Ollama](https://ollama.com) and a Pulse MCP token.

```bash
ollama pull qwen2.5:7b-instruct
cp .env.example .env        # then put your PULSE_MCP_TOKEN in .env
./start.sh                  # Windows: start.bat
```

Open http://localhost:8000, enter a company ID (for example 10600 for Deoleo) and ask away.
Full steps and troubleshooting: [docs/SETUP.md](docs/SETUP.md).

## How an answer is made

| Step | Budget | What happens |
|---|---|---|
| Understand | 10 s | Question gate, glossary check, rule-based parser (local model only as fallback), name lookup in the company's own masters |
| Query | 20 s | SQL built from the metric catalog, checked by the SQL guard, run through Pulse MCP `ch_query` in parallel |
| Answer | 20 s | Chart chosen, summary written from the data, formula note and glossary terms attached |

A hard stop at 55 seconds returns a clear "try a narrower question" message instead of hanging.

## Guardrails

1. **Company lock.** The company ID is verified against Pulse and stored in a signed token. The
   server, not the browser, decides which company each query is for. Changing company clears the chat.
2. **Question gate.** Questions about other companies, company IDs, tenants or prompt tricks are
   refused before any data is touched.
3. **SQL guard.** Every statement is parsed (sqlglot). Only allow-listed tables are allowed, each
   alone in its own SELECT with a top-level `company column = locked ID` filter. Any other company
   ID, table function, write, second statement or bare join is rejected.
4. **Result check.** Returned rows are checked again before anything is shown.

Cooper only ever calls the read-only `ch_query` tool of Pulse MCP.

## Project layout

```
backend/app/        FastAPI server and the answer pipeline
  catalog.py        metrics, formula variants and data sources
  interpreter.py    question -> plan (rules first, local model as fallback)
  sqlbuild.py       plan -> ClickHouse SQL
  guard.py          the guardrails
  pipeline.py       interpret -> query -> answer, chart choice, formula notes
backend/data/       FMCG glossary
backend/tests/      guardrail attacks and all 98 test-sheet questions
frontend/           React + Framer Motion + Recharts UI
scripts/eval_98.py  runs the 98 test questions (live or --dry)
```

## Tests

```bash
source .venv/bin/activate
python -m pytest backend/tests -q            # offline: guardrails + all 98 questions
python scripts/eval_98.py --company 10600 --name Deoleo   # live, writes scripts/out/eval_98.md
```
