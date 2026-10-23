"""Runtime settings, read from environment variables (or a .env file in the repo root)."""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# Pulse MCP: the only data connector Cooper uses.
PULSE_MCP_URL = os.getenv("PULSE_MCP_URL", "https://ado-mcp.fieldassist.io/mcp/data")
PULSE_MCP_TOKEN = os.getenv("PULSE_MCP_TOKEN", "")
# Optional extra headers as "Name: value; Name2: value2" for gateways that need them.
PULSE_MCP_HEADERS = os.getenv("PULSE_MCP_HEADERS", "")

# The shared ClickHouse analytics server that holds most companies.
CH_SERVER = os.getenv("COOPER_CH_SERVER", "analytics")
CH_DATABASE = os.getenv("COOPER_CH_DATABASE", "unify")

# Local LLM via Ollama (no API keys).
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
LLM_ENABLED = os.getenv("COOPER_LLM", "on").lower() not in ("off", "0", "false", "no")

# Time budget in seconds (interpret 10, query 20, answer 20, hard stop 55).
BUDGET_INTERPRET = _int("COOPER_BUDGET_INTERPRET", 10)
BUDGET_QUERY = _int("COOPER_BUDGET_QUERY", 20)
BUDGET_ANSWER = _int("COOPER_BUDGET_ANSWER", 20)
BUDGET_TOTAL = _int("COOPER_BUDGET_TOTAL", 55)

# Signs the company lock. A fresh secret per run is fine for localhost use.
SESSION_SECRET = os.getenv("COOPER_SECRET") or secrets.token_hex(32)

CACHE_TTL_SECONDS = _int("COOPER_CACHE_TTL", 300)
MAX_ROWS = 500

PREFS_DIR = Path(os.getenv("COOPER_PREFS_DIR", DATA_DIR / "prefs"))
