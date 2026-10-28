# Running Cooper AI on your laptop

Everything here is free. Nothing leaves your laptop except the read-only queries Cooper sends
to Pulse MCP.

## 1. Install the tools (once)

| Tool | Version | Check |
|---|---|---|
| Python | 3.10 or newer | `python3 --version` |
| Node.js | 18 or newer | `node --version` |
| Ollama | latest | `ollama --version` |

Then download the local model (about 4.7 GB, once):

```bash
ollama pull qwen2.5:7b-instruct
```

On a laptop with 8 GB RAM or no GPU, the smaller `qwen2.5:3b-instruct` also works. Set
`OLLAMA_MODEL=qwen2.5:3b-instruct` in `.env`. Cooper still works if Ollama isn't running; answers
then use its built-in wording.

## 2. Pulse MCP access

Nothing to set up here. When you open Cooper, click **Connect and authorize Pulse MCP** and sign
in with your FieldAssist ID. Cooper keeps the sign-in on this laptop (`backend/data/auth/`,
git-ignored) and refreshes it automatically. Use **Disconnect** on the start screen to sign out.

Open Cooper at exactly `http://localhost:8000`, because that is where the sign-in page sends you
back. If you use another address, set `COOPER_PUBLIC_URL` in `.env` to match.

## 3. Start

```bash
./start.sh          # macOS / Linux
start.bat           # Windows
```

The first run creates a Python virtual environment, installs packages and builds the web UI
(2 to 4 minutes). After that it starts in a few seconds. Open http://localhost:8000.

## 4. Use it

1. Click **Connect and authorize Pulse MCP** and sign in with your FieldAssist ID (once).
2. Enter the company ID, for example **10600** (Deoleo). Cooper checks it exists in Pulse and
   locks the chat to it.
3. Ask questions in plain English, or tap a suggestion.
4. If Cooper shows a formula choice, pick the one your company uses. It's remembered for that
   company (stored in `backend/data/prefs/`). The **Formulas** tab shows and changes all of them.
5. **How I got this** under each answer shows the period, filters, formulas and the exact SQL.
6. To switch company, use the exit button next to the company name. The chat is cleared.

## Development mode

Run the API and the UI separately with hot reload:

```bash
source .venv/bin/activate
uvicorn backend.app.main:app --reload --port 8000
cd frontend && npm run dev        # http://localhost:5173
```

## Troubleshooting

| You see | Fix |
|---|---|
| "Couldn't reach Pulse MCP" or the sign-in fails | Check you can open https://ado-mcp.fieldassist.io in the browser, then click Connect again. |
| "Your Pulse MCP sign-in has expired" | Click Connect and authorize Pulse MCP again. |
| "Company ID ... wasn't found" | The company isn't on the shared `analytics` server. Dedicated-server companies (Colgate, Haldiram's, HCCB, Mars, BDF, SLMG) aren't supported in this prototype. |
| "The local model isn't running" | Start Ollama (`ollama serve`) or ignore it; answers still work. |
| Slow first answer | Ollama loads the model on first use (10 to 20 seconds). Later answers are fast. |
| Answers time out | Ask for a shorter period or a narrower breakdown. |
