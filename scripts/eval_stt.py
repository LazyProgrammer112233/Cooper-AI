"""Speech-to-text accuracy check: Indian-English, Hinglish and Hindi clips through local Whisper.

    python scripts/eval_stt.py                                  # all models, all modes
    python scripts/eval_stt.py --models large-v3-turbo --modes auto

Clips are made once with Microsoft's neural voices (edge-tts: en-IN Neerja / Prabhat, hi-IN Swara /
Madhur; only these test sentences are sent) and kept in scripts/out/stt/ (git-ignored). For each model
and language mode it reports word error rate (after Cooper's Hindi normalisation, so Devanagari and
Roman spellings compare fairly), whether the question still gives the same plan as the typed one,
whether "Cooper" was heard, and latency. The browser's own recognition can't be scripted offline, so
it isn't scored here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import hindi, pipeline, smalltalk, stt  # noqa: E402
from backend.app.interpreter import parse  # noqa: E402

OUT = ROOT / "scripts" / "out" / "stt"
EN1, EN2, HI1, HI2 = "en-IN-NeerjaNeural", "en-IN-PrabhatNeural", "hi-IN-SwaraNeural", "hi-IN-MadhurNeural"

# (id, kind, voice, what is spoken, what the user means: the typed question)
PHRASES = [
    ("en01", "english", EN1, "Hi Cooper, why are my productive calls low this week?", None),
    ("en02", "english", EN2, "Show UBO for last month.", None),
    ("en03", "english", EN1, "Top 5 brands by secondary sales in North zone.", None),
    ("en04", "english", EN2, "Show primary sales for MTD.", None),
    ("en05", "english", EN1, "Which zone has the lowest coverage?", None),
    ("en06", "english", EN2, "Show TC vs PC trend for the last 6 months.", None),
    ("en07", "english", EN1, "What are sales reps doing wrong?", None),
    ("en08", "english", EN2, "Secondary sales for Figaro brand this month.", None),
    ("en09", "english", EN1, "How many distributors in Maharashtra?", None),
    ("en10", "english", EN2, "Bertolli brand sales last month.", None),
    ("en11", "english", EN1, "Cooper, compare this month versus last month primary sales.", None),
    ("en12", "english", EN2, "Top 10 distributors by primary sales last month.", None),
    ("en13", "english", EN1, "Why did coverage drop in East zone?", None),
    ("en14", "english", EN2, "Show Deoleo secondary sales for yesterday.", None),
    ("hg01", "hinglish", HI1, "कूपर, इस हफ्ते पीसी कम क्यों है?", "Cooper, is hafte PC kam kyun hai?"),
    ("hg02", "hinglish", HI2, "महाराष्ट्र में कितने डिस्ट्रीब्यूटर हैं?", "Maharashtra mein kitne distributor hain?"),
    ("hg03", "hinglish", HI1, "पिछले महीने की प्राइमरी सेल दिखाओ।", "Pichle mahine ki primary sale dikhao."),
    ("hg04", "hinglish", HI2, "सबसे कम कवरेज वाला ज़ोन कौन सा है?", "Sabse kam coverage wala zone kaunsa hai?"),
    ("hg05", "hinglish", HI1, "आज का पीसी कितना है?", "Aaj ka PC kitna hai?"),
    ("hg06", "hinglish", HI2, "नॉर्थ ज़ोन में सेकेंडरी सेल्स कितनी है?", "North zone mein secondary sales kitni hai?"),
    ("hg07", "hinglish", HI1, "सेल्स रिप्स क्या गलत कर रहे हैं?", "Sales reps kya galat kar rahe hain?"),
    ("hg08", "hinglish", HI2, "कूपर, फिगारो ब्रांड की सेल इस महीने।", "Cooper, Figaro brand ki sale is mahine."),
    ("hi01", "hindi", HI1, "इस हफ्ते मेरे प्रोडक्टिव कॉल्स कम क्यों हैं?", None),
    ("hi02", "hindi", HI2, "पिछले महीने की बिक्री दिखाओ।", None),
    ("hi03", "hindi", HI1, "टॉप पांच ब्रांड सेकेंडरी सेल्स के हिसाब से।", None),
    ("hi04", "hindi", HI2, "कवरेज कैसे सुधारें?", None),
    ("hi05", "hindi", HI1, "नमस्ते कूपर, कैसे हो?", None),
    ("hi06", "hindi", HI2, "कल की बिक्री कितनी थी?", None),
]
TODAY = date(2026, 10, 24)
KEYS = ("intent", "metrics", "shape", "dims", "list_dim", "count_only", "order", "limit")


async def make_clips() -> None:
    import edge_tts
    OUT.mkdir(parents=True, exist_ok=True)
    for pid, _, voice, say, _ in PHRASES:
        f = OUT / f"{pid}.mp3"
        if not f.exists():
            await edge_tts.Communicate(say, voice).save(str(f))


def words(text: str) -> list[str]:
    t = hindi.normalize(text)[0].lower()
    return re.findall(r"[a-z0-9]+", t)


def wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[len(hyp)] / max(1, len(ref))


def plan_key(text: str):
    st = smalltalk.analyse(text)
    if st.pure:
        return ("smalltalk", tuple(st.kinds))
    q = st.rest if st.found and smalltalk.has_question(st) else text
    p = parse(pipeline._strip_company(hindi.normalize(q)[0], "Deoleo"), TODAY)
    return tuple([getattr(p, k) if not isinstance(getattr(p, k), list) else tuple(getattr(p, k)) for k in KEYS]
                 + [(p.period.start, p.period.end) if p.period else None,
                    tuple(sorted((f.text.lower(), f.dim_hint) for f in p.filters))])


def company_names() -> list[str]:
    """Deoleo's zone / state / brand names from the running Cooper server, if it's up."""
    try:
        base = "http://127.0.0.1:8000"
        req = urllib.request.Request(base + "/api/company/verify", data=json.dumps({"company_id": 10600}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        tok = json.loads(urllib.request.urlopen(req, timeout=30).read())["token"]
        req = urllib.request.Request(base + "/api/vocab", headers={"X-Company-Token": tok})
        return json.loads(urllib.request.urlopen(req, timeout=60).read())["names"]
    except Exception:
        return ["North", "South", "East", "West", "Maharashtra", "Karnataka", "Figaro", "Bertolli", "Pure", "EV", "EL"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="large-v3-turbo,medium,small")
    ap.add_argument("--modes", default="auto,en,hi")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()
    asyncio.run(make_clips())
    names = company_names()
    audio = {pid: stt.decode((OUT / f"{pid}.mp3").read_bytes()) for pid, *_ in PHRASES}
    stt._cuda_dlls()
    from faster_whisper import WhisperModel

    report = [f"# Speech-to-text check ({len(PHRASES)} clips, {len(names)} company names in the prompt)", ""]
    summary = []
    for model_name in args.models.split(","):
        t = time.monotonic()
        try:
            model = WhisperModel(model_name, device=args.device, compute_type="float16" if args.device == "cuda" else "int8")
        except Exception as exc:
            print(f"{model_name}: could not load ({exc})")
            continue
        engine = stt.Engine()
        engine.model, engine.state, engine.name, engine.device = model, "ready", model_name, args.device
        engine.run(audio["en02"], "en", None)  # warm-up
        print(f"\n== {model_name} on {args.device} (loaded in {time.monotonic() - t:.1f}s)")
        report += [f"## {model_name} ({args.device})", ""]
        for mode in args.modes.split(","):
            rows = []
            for pid, kind, _, say, meant in PHRASES:
                res = engine.transcribe_sync(audio[pid], mode, "Deoleo", names)
                res["text"] = hindi.fix_speech(res["text"])  # what /api/transcribe sends back
                ref = meant or say
                rows.append({"id": pid, "kind": kind, "heard": res["text"], "wer": wer(words(ref), words(res["text"])),
                             "plan": plan_key(res["text"]) == plan_key(ref), "secs": res["seconds"],
                             "audio": res["audio_seconds"], "lang": res["language"],
                             "cooper": ("cooper" not in ref.lower() and "कूपर" not in ref) or
                                       bool(re.search(r"cooper|कूपर", res["text"], re.I))})
            line = [f"{mode:5}"]
            for kind in ("english", "hinglish", "hindi", "all"):
                rs = [r for r in rows if kind == "all" or r["kind"] == kind]
                line.append(f"{kind}: WER {statistics.mean(r['wer'] for r in rs) * 100:5.1f}% plan {sum(r['plan'] for r in rs)}/{len(rs)}")
            lat = [r["secs"] for r in rows]
            cooper = [r for r in rows if re.search(r"cooper|कूपर", next(p[3] for p in PHRASES if p[0] == r["id"]), re.I)]
            line.append(f"Cooper heard {sum(r['cooper'] for r in cooper)}/{len(cooper)}")
            line.append(f"latency avg {statistics.mean(lat):.2f}s max {max(lat):.2f}s for {statistics.mean(r['audio'] for r in rows):.1f}s clips")
            print("  " + " | ".join(line))
            summary.append((model_name, mode, line))
            report += [f"### mode {mode}", "", " | ".join(line), "", "| id | heard | WER | plan |", "|---|---|---|---|"]
            report += [f"| {r['id']} | {r['heard']} | {r['wer'] * 100:.0f}% | {'yes' if r['plan'] else 'no'} |" for r in rows]
            report.append("")
        del model
    (OUT.parent / "eval_stt.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\nReport: {OUT.parent / 'eval_stt.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
