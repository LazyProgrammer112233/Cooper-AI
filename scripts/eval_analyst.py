"""Runs the 105 analyst test prompts against a running Cooper server (live Pulse data) and saves the answers.

    python scripts/eval_analyst.py                 # all prompts, company 10600
    python scripts/eval_analyst.py --only 1,21,86  # some of them

Writes scripts/out/eval_analyst.json (every answer, with timings and the constraints Cooper understood)
and scripts/out/eval_analyst.md (a readable report). Needs Cooper on localhost:8000 and a Pulse sign-in.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


def post(path: str, body: dict, token: str | None = None, timeout: int = 120) -> str:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-Company-Token": token} if token else {})})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")


def ask(q: str, token: str) -> tuple[dict, float]:
    t = time.monotonic()
    body = post("/api/chat", {"question": q}, token)
    line = [x for x in body.split("\n") if x.startswith("data: ") and '"type": "answer"' in x][-1]
    return json.loads(line[6:])["answer"], round(time.monotonic() - t, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", type=int, default=10600)
    ap.add_argument("--only")
    args = ap.parse_args()
    for _ in range(120):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=3)
            break
        except Exception:
            time.sleep(1)
    token = json.loads(post("/api/company/verify", {"company_id": args.company}))["token"]
    items = json.loads((ROOT / "backend/tests/fixtures/test_sheet_analyst.json").read_text(encoding="utf-8"))
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        items = [i for i in items if i["id"] in wanted]
    out_dir = ROOT / "scripts" / "out"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "eval_analyst.json"
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for it in items:
        try:
            a, secs = ask(it["prompt"], token)
        except Exception as exc:  # report and carry on
            a, secs = {"kind": "crash", "summary": f"{type(exc).__name__}: {exc}"}, 0.0
        results[str(it["id"])] = {"id": it["id"], "category": it["category"], "prompt": it["prompt"], "kind": a.get("kind"),
                                  "seconds": secs, "summary": a.get("summary"), "notes": a.get("notes", []),
                                  "constraints": (a.get("how") or {}).get("constraints", []),
                                  "queries": (a.get("how") or {}).get("queries"),
                                  "rows": len(((a.get("table") or {}).get("rows")) or []),
                                  "sections": len(a.get("sections") or []), "rca": bool(a.get("rca"))}
        print(f"{it['id']:>3} {str(a.get('kind')):<8} {secs:>5}s  {str(a.get('summary'))[:150]}", flush=True)
        path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# Cooper analyst prompts: live run on company {args.company}", ""]
    for k in sorted(results, key=int):
        r = results[k]
        lines += [f"## {r['id']}. {r['prompt']}", "", f"**{r['kind']}** · {r['seconds']}s · {r['queries']} queries · {r['rows']} rows", "",
                  str(r["summary"]), ""]
        lines += [f"- Understood: {c}" for c in r["constraints"]]
        lines += [f"- Note: {n}" for n in r["notes"]]
        lines.append("")
    (out_dir / "eval_analyst.md").write_text("\n".join(lines), encoding="utf-8")
    secs = [r["seconds"] for r in results.values() if r["seconds"]]
    print(f"\n{len(results)} answers; median {sorted(secs)[len(secs) // 2] if secs else 0}s, max {max(secs) if secs else 0}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
