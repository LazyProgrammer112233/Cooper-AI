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

You need Python 3.10+, Node 18+, [Ollama](https://ollama.com) and a FieldAssist ID with Pulse MCP access.

```bash
ollama pull qwen2.5:7b-instruct
./start.sh                  # Windows: start.bat
```

Open http://localhost:8000, click **Connect and authorize Pulse MCP**, sign in with your
FieldAssist ID, then enter a company ID (for example 10600 for Deoleo) and ask away.
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

## "Why" questions (root cause analysis)

Ask "Why are my productive calls low this week?", "Why is primary sales down?", "What are sales reps
doing wrong?" or "How can we improve coverage?". Cooper then:

1. **Compares like for like.** This week so far vs the same weekdays last week, MTD vs the same days
   last month, last month vs the month before. Today is left out while its field data is syncing.
2. **Builds a driver tree.** For example PC = reps in market × days per rep × calls per rep-day × PC %,
   or primary = distributors billed × billing per distributor. Each driver's share of the change comes
   from a log decomposition, so the shares add up to 100%. It also checks first-call time, late starts,
   zero-order days, lines per call, beat and tour-plan adherence, UBO and drop size.
3. **Finds where it moved.** It lists the zones, regions, reps, distributors and categories with the
   biggest drops (or gains), and the reps to coach first.
4. **Reads the calendar.** It counts working days and flags holidays, festivals and seasons from
   `backend/data/india_calendar.json` (2025 to 2027, editable). Day-starts marked Holiday or Leave in
   the field app turn a calendar guess into a fact. Calendar-only explanations are labelled "likely".
5. **Suggests what to do.** Each finding maps to concrete actions in `backend/data/rca_playbook.json`.

Every number comes from guarded Pulse queries (about 15 to 20, run in parallel, usually under 15 s).
The answer text is a template. The local model can reword it (`COOPER_RCA_LLM=on`), but this is off by
default: small models flip causes around while keeping the numbers, which the number check can't catch.

## Hindi and Hinglish

Ask in English, Hindi (Devanagari) or Hinglish: "is hafte PC kitna hai", "pichle mahine ki primary sale
dikhao", "sabse kam coverage wala zone kaunsa hai", "Maharashtra mein kitne distributor hain",
"इस हफ्ते मेरे प्रोडक्टिव कॉल्स कम क्यों हैं". `backend/app/hindi.py` turns these into the English the parser
reads, using `backend/data/hindi_lexicon.json` (question words, verbs, time, FMCG terms, Devanagari spellings
of English words, Indian states and cities, number words; editable). Unknown Devanagari names are
transliterated so they can still be matched against the company's masters. The guardrails check both the
original and the translated text. Answers stay in English, plus one Hindi line built from the same
numbers, and "How I got this" shows what Cooper understood.

Greetings and small talk ("hi Cooper", "namaste", "kaise ho", "thanks", "bahut kaam hai") get a warm
reply in the same language without touching any data. A greeting with a question ("kaise ho Cooper,
aaj PC kam aa raha hai") gets a one-line hello and then the normal answer.

## Voice input

Click the mic and speak in English, Hindi or Hinglish. Cooper transcribes it **on this laptop** with
Whisper (`large-v3-turbo` via faster-whisper, on the NVIDIA GPU if there is one, otherwise the CPU). It's
free, works offline after a one-time model download, and the audio never leaves the machine.

- The language switch next to the mic cycles **Auto → EN → हिं** (remembered). Auto decodes clearly
  English speech as English and everything else Hindi-steered, which writes Hindi in Devanagari and
  keeps English words in Latin letters ("North Zone में secondary sales"); Cooper reads both.
- Whisper gets a short hint list: "Cooper", the company name, its zones, states, brands and categories,
  and FMCG terms (PC, TC, UBO, MTD, primary/secondary sales, lakh, crore...). Near-miss names are
  snapped back ("Figuero" → Figaro) and common mishearings fixed (`backend/data/speech_fixes.json`).
- No invented words: voice-activity detection drops silence, decoding runs at temperature 0 without
  carrying text over, low-confidence segments are dropped, and phrases like "thank you for watching"
  are removed. Pure noise or silence comes back empty.
- Recording stops after 2.5 s of silence (30 s max). The text then shows in the box for 1 s before
  it is sent; typing, Esc or "cancel" stops it.
- While the model loads (a few seconds after start), the mic falls back to the browser's recognition.

Settings (in `.env`, all optional): `COOPER_STT=on|off`, `COOPER_STT_MODEL` (`large-v3-turbo`,
`medium`, `small`), `COOPER_STT_DEVICE` (`auto`, `cuda`, `cpu`). Check accuracy with
`python scripts/eval_stt.py` (28 Indian-English, Hinglish and Hindi clips; the report goes to
`scripts/out/eval_stt.md`).

## Project layout

```
backend/app/        FastAPI server and the answer pipeline
  catalog.py        metrics, formula variants and data sources
  interpreter.py    question -> plan (rules first, local model as fallback)
  sqlbuild.py       plan -> ClickHouse SQL
  guard.py          the guardrails
  pipeline.py       interpret -> query -> answer, chart choice, formula notes
  rca.py            "why" questions: comparison windows, driver tree, contributors, playbook
  indcal.py         Indian holidays, festivals, seasons and working days
  hindi.py          Hindi / Hinglish / misheard speech -> English, and the Hindi answer line
  smalltalk.py      greetings and small talk
  stt.py            local Whisper speech-to-text for the mic
backend/data/       FMCG glossary, Indian calendar, RCA playbook, Hindi lexicon, speech fixes
backend/tests/      guardrail attacks and all 98 test-sheet questions
frontend/           React + Framer Motion + Recharts UI
scripts/eval_98.py  runs the 98 test questions (live or --dry)
scripts/eval_stt.py speech-to-text accuracy and latency check
```

## Tests

```bash
source .venv/bin/activate
python -m pytest backend/tests -q            # offline: guardrails + all 98 questions
python scripts/eval_98.py --company 10600 --name Deoleo   # live, writes scripts/out/eval_98.md
```
