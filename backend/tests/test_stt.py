"""Voice input: the /api/transcribe endpoint, its limits, the Whisper prompt and the hallucination filter."""

import io
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app import config, main, stt  # noqa: E402
from backend.app.guard import sign_company  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "STT_ENABLED", False)  # never load the real model in tests
    seen = {}

    async def names(ctx):
        seen["company"] = ctx.id
        return ["North", "Maharashtra", "Figaro"] if ctx.id == 10600 else ["Other Co Zone"]
    monkeypatch.setattr(main, "_company_names", names)
    with TestClient(main.app) as c:
        c.seen = seen
        yield c


def _wav(seconds=1.0, rate=8000, channels=2) -> bytes:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    frames = np.repeat(tone[:, None], channels, axis=1).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


TOKEN = sign_company(10600, "Deoleo")


def test_transcribe_needs_a_company(client):
    r = client.post("/api/transcribe", content=_wav(), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 401
    r = client.post("/api/transcribe", content=_wav(), headers={"Content-Type": "audio/wav", "X-Company-Token": "bad.sig"})
    assert r.status_code == 401


def test_transcribe_limits(client, monkeypatch):
    h = {"Content-Type": "audio/wav", "X-Company-Token": TOKEN}
    assert client.post("/api/transcribe", content=b"x" * (stt.MAX_BYTES + 1), headers=h).status_code == 413
    assert client.post("/api/transcribe?mode=fr", content=_wav(), headers=h).status_code == 400
    assert client.post("/api/transcribe", content=b"", headers=h).status_code == 400
    r = client.post("/api/transcribe", content=_wav(), headers={**h, "Content-Type": "text/plain"})
    assert r.status_code == 415
    monkeypatch.setattr(stt.engine, "state", "loading")
    assert client.post("/api/transcribe", content=_wav(), headers=h).status_code == 503


def test_transcribe_uses_only_the_locked_company_and_fixes_speech(client, monkeypatch):
    got = {}

    async def fake(data, mode, company, names):
        got.update(mode=mode, company=company, names=names, size=len(data))
        return {"text": "show you be oh for last month in north zone", "language": "en", "seconds": 0.4,
                "audio_seconds": 1.0, "dropped_segments": 0}
    monkeypatch.setattr(stt.engine, "state", "ready")
    monkeypatch.setattr(stt.engine, "transcribe", fake)
    r = client.post("/api/transcribe?mode=hi", content=_wav(), headers={"Content-Type": "audio/wav", "X-Company-Token": TOKEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "show UBO for last month in north zone"
    assert body["raw"].startswith("show you be oh")
    assert got["company"] == "Deoleo" and got["mode"] == "hi" and "Other Co Zone" not in got["names"]
    assert client.seen["company"] == 10600


def test_too_long_recording_is_refused(client, monkeypatch):
    monkeypatch.setattr(stt.engine, "state", "ready")
    long_wav = _wav(seconds=stt.MAX_SECONDS + 3, rate=8000, channels=1)
    assert len(long_wav) < stt.MAX_BYTES
    r = client.post("/api/transcribe", content=long_wav, headers={"Content-Type": "audio/wav", "X-Company-Token": TOKEN})
    assert r.status_code == 400 and "30 seconds" in r.json()["detail"]


def test_health_reports_speech_model(client):
    assert set(client.get("/api/health").json()["stt"]) >= {"state", "model", "device"}


# --------------------------------------------------------------------------- pieces

def test_decode_wav_to_16k_mono():
    a = stt.decode(_wav(seconds=1.0, rate=8000, channels=2))
    assert a.dtype == np.float32 and abs(len(a) - 16000) <= 2 and float(np.abs(a).max()) <= 1.0


@pytest.mark.parametrize("raw, clean", [
    ("Show UBO for last month. Thank you for watching.", "Show UBO for last month."),
    ("primary sales please subscribe", "primary sales"),
    ("the the the the sales", "the sales"),
    ("इस हफ्ते पीसी कम क्यों है धन्यवाद।", "इस हफ्ते पीसी कम क्यों है"),
    ("Thank you.", ""),
    ("[Music] Cooper, show PC trend", "Cooper, show PC trend"),
    ("Show TC trend Show TC trend Show TC trend", "Show TC trend"),
    ("Thank you Cooper, show primary sales", "Thank you Cooper, show primary sales"),
])
def test_hallucination_filter(raw, clean):
    assert stt.clean(raw) == clean


def test_prompt_builder():
    p = stt.build_prompt("Deoleo", ["NORTH", "Maharashtra", "0", "Deoleo", "maharashtra", "Figaro"] + [f"Zone {i}" for i in range(200)])
    assert p.startswith(stt.HINGLISH_STYLE) and "Cooper, Deoleo." in p and "UBO" in p
    assert "North" in p and "Figaro" in p and ", 0" not in p
    assert p.lower().count("maharashtra") == 1 and p.count("Deoleo") == 1
    assert len(p) <= 600
    assert not stt.build_prompt("Deoleo", ["North"], mode="en").startswith(stt.HINGLISH_STYLE)


def test_low_confidence_segments_are_dropped():
    seg = lambda text, lp, cr=1.2, ns=0.1: SimpleNamespace(text=text, avg_logprob=lp, compression_ratio=cr, no_speech_prob=ns)

    class FakeModel:
        def transcribe(self, audio, **kw):
            assert kw["vad_filter"] and kw["temperature"] == 0.0 and kw["condition_on_previous_text"] is False
            assert kw["beam_size"] == 5 and "Cooper" in kw["initial_prompt"]
            return iter([seg(" Why are my productive calls low", -0.3), seg(" this week?", -0.4),
                         seg(" subscribe to my channel", -1.6), seg(" la la la la la", -0.2, cr=3.1),
                         seg(" hmm", -0.5, ns=0.9)]), SimpleNamespace(language="en", language_probability=0.97)
    e = stt.Engine()
    e.model, e.state = FakeModel(), "ready"
    out = e.transcribe_sync(np.zeros(16000, dtype=np.float32), "en", "Deoleo", ["North"])
    assert out["text"] == "Why are my productive calls low this week?"
    assert out["dropped_segments"] == 3 and out["language"] == "en"


def test_auto_mode_picks_english_or_hindi_steered_decoding():
    calls = []

    class FakeModel:
        def __init__(self, lang, prob):
            self.det = (lang, prob, [])

        def detect_language(self, audio, **kw):
            return self.det

        def transcribe(self, audio, **kw):
            calls.append((kw["language"], kw["initial_prompt"]))
            return iter([SimpleNamespace(text=" ok", avg_logprob=-0.2, compression_ratio=1.1, no_speech_prob=0.1)]), \
                SimpleNamespace(language=kw["language"] or "en", language_probability=0.9)
    e = stt.Engine()
    e.state = "ready"
    for det, want in [(("en", 0.95), "en"), (("hi", 0.7), "hi"), (("en", 0.6), "hi"), (("ur", 0.8), "hi")]:
        calls.clear()
        e.model = FakeModel(*det)
        e.transcribe_sync(np.zeros(16000, dtype=np.float32), "auto", "Deoleo", [])
        assert calls[0][0] == want
        assert (stt.DEVANAGARI_STYLE in calls[0][1]) == (want == "hi")


def test_urdu_script_is_retried_in_devanagari():
    outs = iter([" سیلز کم کیوں ہے", " सेल्स कम क्यों है"])
    langs = []

    class FakeModel:
        def transcribe(self, audio, **kw):
            langs.append(kw["language"])
            return iter([SimpleNamespace(text=next(outs), avg_logprob=-0.2, compression_ratio=1.1, no_speech_prob=0.1)]), \
                SimpleNamespace(language="hi", language_probability=0.9)
    e = stt.Engine()
    e.model, e.state = FakeModel(), "ready"
    out = e.transcribe_sync(np.zeros(16000, dtype=np.float32), "en", "Deoleo", [])
    assert out["text"] == "सेल्स कम क्यों है" and langs == ["en", "hi"]


@pytest.mark.parametrize("heard, fixed", [("Secondary sales for Figuero brand", "Secondary sales for Figaro brand"),
                                          ("Bertoli sales", "Bertolli sales"), ("Show sales North zone", "Show sales North zone")])
def test_near_miss_names_are_snapped(heard, fixed):
    assert stt.snap_names(heard, ["Figaro", "Bertolli", "NORTH", "Maharashtra"]) == fixed
