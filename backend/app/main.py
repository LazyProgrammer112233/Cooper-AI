"""Cooper AI web server (localhost only).

    uvicorn backend.app.main:app --port 8000

The browser must first lock a company (POST /api/company/verify). Every later call carries the
signed company token, and the server, never the browser, decides which company a query is for.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, glossary, hindi, pipeline, prefs, stt
from .catalog import METRICS
from .guard import GuardViolation, check_sql, sign_company, verify_company_token
from .llm import LocalLLM
from .mcp_auth import auth as pulse_auth
from .mcp_client import DataError, PulseMCP
from .sqlbuild import build_member_list

app = FastAPI(title="Cooper AI", docs_url=None, redoc_url=None)
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "dist"

_llm = LocalLLM()
_mcp = PulseMCP()


@app.on_event("startup")
async def _load_speech_model():
    stt.engine.start()  # background thread: the server answers right away, voice is ready a little later


class VerifyIn(BaseModel):
    company_id: int = Field(gt=0, lt=10_000_000)


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class FormulaIn(BaseModel):
    metric: str
    variant: str | None = None  # None resets to the basic formula


def _company_sql(cid: int) -> str:
    return (
        "SELECT * FROM (SELECT toInt64(any(Id)) AS id, any(Name) AS name, toInt64(any(Deleted)) AS deleted, "
        f"count() AS found FROM (SELECT * FROM master_companies FINAL WHERE Id = {cid}) AS x) AS c "
        "CROSS JOIN (SELECT toInt64(any(offset_minutes)) AS tz, count() AS tz_found FROM "
        f"(SELECT * FROM client_timezones WHERE client_id = {cid}) AS y) AS t "
        "CROSS JOIN (SELECT toInt64(any(fiscal_year_start_month)) AS fy, count() AS fy_found FROM "
        f"(SELECT * FROM client_fiscal_config WHERE client_id = {cid}) AS z) AS f "
        "CROSS JOIN (SELECT toInt64(max(OrderDateKey)) AS last_day FROM (SELECT * FROM SecondaryDemandSale "
        f"WHERE CompanyId = {cid} AND OrderDateKey >= {(date.today().year - 1) * 10000 + 101}) AS w) AS d"
    )


def _ctx(token: str | None) -> pipeline.CompanyCtx:
    """Layer 1: the company comes only from the signed token."""
    if not token:
        raise HTTPException(401, "Select a company first.")
    try:
        data = verify_company_token(token)
    except GuardViolation as exc:
        raise HTTPException(401, str(exc)) from exc
    return pipeline.CompanyCtx(id=data["cid"], name=data["name"], tz_offset=data.get("tz", 330),
                               fy_start_month=data.get("fy", 4))


@app.get("/api/mcp/status")
async def mcp_status():
    return {**pulse_auth.status(), "static_token": bool(config.PULSE_MCP_TOKEN)}


@app.post("/api/mcp/connect")
async def mcp_connect():
    """Starts the FieldAssist sign-in for Pulse MCP and returns the URL to open."""
    try:
        return await pulse_auth.start()
    except Exception as exc:
        from .mcp_auth import _short
        raise HTTPException(502, f"Couldn't start the Pulse MCP sign-in: {_short(exc)}") from exc


@app.get("/api/mcp/callback")
async def mcp_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                       error_description: str | None = None):
    result = await pulse_auth.finish(code, state, error_description or error)
    if result["connected"]:
        return RedirectResponse("/?mcp=connected", status_code=303)
    from urllib.parse import quote
    return RedirectResponse(f"/?mcp=error&message={quote(result.get('error') or 'Sign-in failed')}", status_code=303)


@app.post("/api/mcp/disconnect")
async def mcp_disconnect():
    pulse_auth.disconnect()
    pipeline._CACHE.clear()
    return pulse_auth.status()


@app.post("/api/company/verify")
async def verify_company(body: VerifyIn):
    cid = body.company_id
    sql = _company_sql(cid)
    check_sql(sql, cid)
    try:
        async with _mcp.session() as s:
            res = await s.ch_query(sql, top=1, timeout=15)
    except DataError as exc:
        raise HTTPException(502, str(exc)) from exc
    row = res.rows[0] if res.rows else {}
    if not row.get("found") or row.get("deleted"):
        raise HTTPException(404, f"Company ID {cid} wasn't found, or it isn't active on the shared analytics server.")
    if not row.get("last_day"):
        raise HTTPException(404, f"Company ID {cid} has no sales data in the last year on the shared analytics server.")
    name = str(row["name"]).strip()
    tz = int(row["tz"]) if row.get("tz_found") else 330
    fy = int(row["fy"]) if row.get("fy_found") and 1 <= int(row["fy"]) <= 12 else 4
    token = sign_company(cid, name, tz, fy)
    last = str(row["last_day"])
    return {"token": token, "company": {"id": cid, "name": name, "tz_offset": tz, "fy_start_month": fy,
                                        "last_data": f"{last[:4]}-{last[4:6]}-{last[6:]}"}}


@app.post("/api/chat")
async def chat(body: ChatIn, x_company_token: str | None = Header(default=None)):
    ctx = _ctx(x_company_token)

    async def stream():
        try:
            async for ev in pipeline.run(body.question, ctx, llm=_llm, mcp=_mcp):
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # never leak a stack trace or cut the stream (MCP can raise exception groups)
            err = {"type": "answer", "answer": {
                "kind": "error", "question": body.question, "chart": None, "table": None, "formula_notes": [],
                "notes": [], "terms": [], "suggestions": [body.question],
                "summary": f"Something went wrong while getting the data ({type(exc).__name__}). Please ask again."}}
            yield f"data: {json.dumps(err)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/formulas")
async def get_formulas(x_company_token: str | None = Header(default=None)):
    ctx = _ctx(x_company_token)
    saved = prefs.load(ctx.id)
    out = []
    for m in METRICS.values():
        if not m.ambiguous:
            continue
        out.append({"metric": m.id, "label": m.label, "chosen": saved.get(m.id, m.default.id), "saved": m.id in saved,
                    "options": [{"id": v.id, "label": v.label, "formula": v.formula} for v in m.variants[:4]]})
    return {"formulas": out}


@app.post("/api/formulas")
async def set_formula(body: FormulaIn, x_company_token: str | None = Header(default=None)):
    ctx = _ctx(x_company_token)
    try:
        saved = prefs.reset(ctx.id, body.metric) if body.variant is None else prefs.save(ctx.id, body.metric, body.variant)
    except KeyError as exc:
        raise HTTPException(400, f"Unknown formula: {exc}") from exc
    pipeline._CACHE.clear()
    return {"saved": saved}


_VOCAB: dict[int, tuple[float, list[str]]] = {}


async def _company_names(ctx: pipeline.CompanyCtx) -> list[str]:
    """The locked company's zone / region / state / city / brand / category names (guarded queries, cached 30 min)."""
    import time
    hit = _VOCAB.get(ctx.id)
    if hit and time.monotonic() - hit[0] < 1800:
        return hit[1]
    names: list[str] = []
    try:
        async with _mcp.session() as session:
            for dim in ("zone", "region", "state", "brand", "category", "city"):
                rows = await pipeline._query(session, build_member_list(ctx.id, dim, limit=60), ctx.id, timeout=10)
                names += [n for n in (str(r.get("name") or "").strip() for r in rows) if len(n) >= 2 and not n.isdigit()]
        names = list(dict.fromkeys(names + [ctx.name]))
        _VOCAB[ctx.id] = (time.monotonic(), names)
    except Exception:  # vocab is a nicety: never block the mic on it
        names = [ctx.name]
    return names


@app.get("/api/vocab")
async def vocab(x_company_token: str | None = Header(default=None)):
    """Words the voice input should favour: this company's names, FMCG terms, Hindi words, speech fixes."""
    ctx = _ctx(x_company_token)
    names = sorted(set(await _company_names(ctx)))
    terms = sorted({t.term for t in glossary.terms()} | {a for t in glossary.terms() for a in t.aliases}
                   | {m.label for m in METRICS.values()})
    lx = hindi.lexicon()
    words = sorted({k for key in ("phrases", "words", "roman_only", "places") for k in lx[key]})
    fixes = json.loads((config.DATA_DIR / "speech_fixes.json").read_text(encoding="utf-8"))["fixes"]
    return {"names": names, "terms": terms, "hindi": words, "fixes": fixes}


AUDIO_TYPES = ("audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp4",
               "application/octet-stream")


@app.post("/api/transcribe")
async def transcribe(request: Request, mode: str = "auto", x_company_token: str | None = Header(default=None)):
    """Voice question -> text with local Whisper, biased towards Cooper, the locked company's names and FMCG terms."""
    ctx = _ctx(x_company_token)
    if mode not in ("auto", "en", "hi"):
        raise HTTPException(400, "mode must be auto, en or hi")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and ctype not in AUDIO_TYPES:
        raise HTTPException(415, "Send the recording as WebM/Opus, Ogg or WAV audio.")
    if int(request.headers.get("content-length") or 0) > stt.MAX_BYTES:
        raise HTTPException(413, "Recording is too large (2 MB max, about 30 seconds).")
    data = await request.body()
    if len(data) > stt.MAX_BYTES:
        raise HTTPException(413, "Recording is too large (2 MB max, about 30 seconds).")
    if not data:
        raise HTTPException(400, "Empty recording.")
    if stt.engine.state != "ready":
        raise HTTPException(503, f"The speech model is {stt.engine.state}." + (" " + stt.engine.error if stt.engine.error else ""))
    try:
        res = await stt.engine.transcribe(data, mode, ctx.name, await _company_names(ctx))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # corrupt audio and the like
        raise HTTPException(400, f"Couldn't read that recording ({type(exc).__name__}).") from exc
    res["raw"] = res["text"]
    res["text"] = hindi.fix_speech(res["text"])
    return res


@app.get("/api/glossary")
async def get_glossary():
    return {"terms": [t.__dict__ for t in glossary.terms()]}


@app.get("/api/health")
async def health():
    llm = await _llm.health()
    return {"ok": True, "llm": llm, "mcp": {**pulse_auth.status(), "static_token": bool(config.PULSE_MCP_TOKEN)},
            "stt": stt.engine.status()}


if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        f = FRONTEND / path
        if path and f.is_file() and FRONTEND in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(FRONTEND / "index.html")
