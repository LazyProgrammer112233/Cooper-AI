"""Local speech-to-text with Whisper (faster-whisper). Free, offline, and the audio never leaves the laptop.

    stt.engine.start()                       # load the model in a background thread at server start
    text = await stt.engine.transcribe(audio_bytes, mode="auto", prompt=stt.build_prompt("Deoleo", names))

Whisper is multilingual, so English, Hindi and Hinglish all work with one model. Accuracy settings:
- the prompt lists "Cooper", the company and its zone / state / brand names plus FMCG terms, so those
  words are spelled right;
- voice-activity detection cuts silence, decoding is greedy-free (beam 5) at temperature 0 and never
  conditions on earlier text, and low-confidence or repetitive segments are dropped, so Whisper does
  not invent words ("thank you for watching") in pauses.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import threading
import time
import wave
from pathlib import Path

import numpy as np

from . import config

SAMPLE_RATE = 16000
MAX_SECONDS = 30
MAX_BYTES = 2 * 1024 * 1024


def _cuda_dlls() -> None:
    """Make the pip-installed CUDA libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12) loadable on Windows."""
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    for base in list(getattr(nvidia, "__path__", [])):
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            d = Path(base) / sub / "bin"
            if d.is_dir():
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(str(d))
                    except OSError:
                        pass


# --------------------------------------------------------------------------- audio


def decode(data: bytes) -> np.ndarray:
    """Any browser recording (WAV, WebM/Opus, Ogg, MP3) -> 16 kHz mono float32."""
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError("Only 16-bit WAV is supported")
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        if rate != SAMPLE_RATE:
            n = int(len(a) * SAMPLE_RATE / rate)
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
        return a
    import av  # bundled FFmpeg: no system install needed
    out = []
    with av.open(io.BytesIO(data)) as c:
        rs = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in c.decode(audio=0):
            for f in rs.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in rs.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    if not out:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(out).astype(np.float32) / 32768.0


# --------------------------------------------------------------------------- prompt and clean-up

_ARABIC = re.compile(r"[\u0600-\u06FF]")
DEVANAGARI_STYLE = "कूपर, इस हफ्ते पीसी कम क्यों है? पिछले महीने की प्राइमरी सेल दिखाओ।"

FMCG_TERMS = ("PC, TC, UBO, MTD, LMTD, primary sales, secondary sales, productive calls, coverage, distributor, outlet, "
              "beat, SKU, lakh, crore")
HINGLISH_STYLE = "Cooper, is hafte PC kam kyun hai? Pichle mahine ki primary sale dikhao."


def build_prompt(company: str, names: list[str], mode: str = "auto", max_chars: int = 600) -> str:
    """Short vocabulary prompt (well under Whisper's 224-token limit): Cooper, the company, FMCG terms, its names."""
    head = f"Cooper, {company}. {FMCG_TERMS}."
    if mode in ("auto", "hinglish"):
        head = f"{HINGLISH_STYLE} {head}"
    elif mode == "hi":
        head = f"{DEVANAGARI_STYLE} {head}"
    seen, picked = {company.lower(), "cooper"}, []
    for n in names:
        n = re.sub(r"\s+", " ", str(n)).strip()
        if len(n) < 3 or n.isdigit() or n.lower() in seen:
            continue
        seen.add(n.lower())
        picked.append(n.title() if n.isupper() else n)
    text = head
    for n in picked:
        if len(text) + len(n) + 2 > max_chars:
            break
        text += (" " if text.endswith(".") else ", ") + n
    return text


_HALLUCINATIONS = [
    r"thank(s| you)( so much)? for watching\.?", r"please (like and )?subscribe\.?", r"subscribe to (my|our|the) channel\.?",
    r"like,? share and subscribe\.?", r"see you (in the )?next (video|time)\.?", r"thanks for listening\.?",
    r"subtitles? by .*$", r"transcribed by .*$", r"amara\.org.*$", r"www\.\S+", r"♪+", r"\[(music|applause|silence)\]",
    r"\((music|applause|silence)\)", r"(धन्यवाद|शुक्रिया)[।.!]?\s*$", r"^\s*(thank you|thanks)[.!]?\s*$",
]
_HALL_RX = [re.compile(p, re.I) for p in _HALLUCINATIONS]


def clean(text: str) -> str:
    t = text.strip()
    for rx in _HALL_RX:
        t = rx.sub(" ", t)
    t = re.sub(r"\b(\w+)(?:[\s,]+\1\b){2,}", r"\1", t, flags=re.I)  # "the the the" -> "the"
    t = re.sub(r"(.{6,40}?)(?:\s*\1){2,}", r"\1", t)  # a phrase looping three or more times
    return re.sub(r"\s+", " ", t).strip(" ,")


def _lev(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


_COMMON = set("""the and for with from this that what which show tell sales sale primary secondary productive calls total
value order orders zone zones region regions state states city brand brands category distributor distributors outlet
outlets team coverage month week year today last trend compare north south east west top bottom lowest highest why
kaise kitne kitna kitni dikhao batao hafte mahine pichle sabse galat kyun""".split())


def snap_names(text: str, names: list[str]) -> str:
    """Words one or two letters off a company name become that name: 'Figuero' -> 'Figaro'."""
    single = {}
    for n in names:
        n = str(n).strip()
        if " " not in n and len(n) >= 4 and not n.isdigit():
            single.setdefault(n.lower(), n.title() if n.isupper() else n)

    def fix(m: re.Match) -> str:
        w = m.group(0)
        lw = w.lower()
        if lw in single or lw in _COMMON or len(lw) < 5:
            return single.get(lw, w) if lw in single else w
        limit = 2 if len(lw) >= 7 else 1
        best, best_d = None, limit + 1
        for k, v in single.items():
            if k[0] != lw[0]:
                continue  # names keep their first letter when misheard
            d = _lev(lw, k, limit)
            if d < best_d:
                best, best_d = v, d
        return best if best and best_d <= limit else w
    return re.sub(r"[A-Za-z]{4,}", fix, text)


# --------------------------------------------------------------------------- engine


class Engine:
    def __init__(self):
        self.model = None
        self.state = "off"  # off | loading | ready | error
        self.error: str | None = None
        self.device = ""
        self.name = config.STT_MODEL
        self.load_seconds = 0.0
        self._lock = threading.Lock()

    def status(self) -> dict:
        return {"enabled": config.STT_ENABLED, "state": self.state, "model": self.name, "device": self.device,
                "error": self.error}

    def start(self) -> None:
        if not config.STT_ENABLED or self.state in ("loading", "ready"):
            return
        self.state = "loading"
        threading.Thread(target=self._load, name="stt-load", daemon=True).start()

    def _load(self) -> None:
        t = time.monotonic()
        try:
            _cuda_dlls()
            from faster_whisper import WhisperModel
            model, device = None, ""
            if config.STT_DEVICE in ("auto", "cuda"):
                try:
                    model = WhisperModel(self.name, device="cuda", compute_type="float16")
                    model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")  # loads CUDA kernels now
                    device = "cuda"
                except Exception as exc:  # no GPU / missing CUDA libraries: CPU still works
                    model, self.error = None, f"GPU unavailable ({type(exc).__name__}), using CPU"
            if model is None:
                model = WhisperModel(self.name, device="cpu", compute_type="int8",
                                     cpu_threads=max(4, (os.cpu_count() or 8) // 2))
                device = "cpu"
            self.model, self.device, self.state = model, device, "ready"
            if device == "cuda":
                self.error = None
        except Exception as exc:
            self.state, self.error = "error", f"{type(exc).__name__}: {exc}"[:300]
        self.load_seconds = round(time.monotonic() - t, 1)

    def run(self, audio: np.ndarray, language: str | None, prompt: str | None, hotwords: str | None = None) -> dict:
        """Blocking transcription of 16 kHz audio. `language` None = auto-detect."""
        if self.state != "ready":
            raise RuntimeError("Speech model is not ready")
        t = time.monotonic()
        with self._lock:  # one GPU model: one job at a time
            segments, info = self.model.transcribe(
                audio, language=language, task="transcribe", beam_size=5, temperature=0.0,
                condition_on_previous_text=False, initial_prompt=prompt or None, hotwords=hotwords or None,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
                no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
                without_timestamps=True)
            kept, dropped = [], 0
            for s in segments:
                if s.avg_logprob < -1.0 or s.compression_ratio > 2.4 or s.no_speech_prob > 0.6:
                    dropped += 1
                    continue
                kept.append(s.text)
        return {"text": clean(" ".join(kept)), "language": info.language,
                "language_probability": round(float(info.language_probability), 2),
                "audio_seconds": round(len(audio) / SAMPLE_RATE, 2), "seconds": round(time.monotonic() - t, 2),
                "dropped_segments": dropped}

    def detect(self, audio: np.ndarray) -> tuple[str, float]:
        with self._lock:
            try:
                lang, prob, _ = self.model.detect_language(audio, vad_filter=True)
            except TypeError:  # older faster-whisper
                lang, prob, _ = self.model.detect_language(audio)
        return lang, float(prob)

    def transcribe_sync(self, audio: np.ndarray, mode: str, company: str, names: list[str]) -> dict:
        """mode: auto | en | hi. See AUTO below for how auto handles Hinglish."""
        hot = f"Cooper {company}"
        lang = {"en": "en", "hi": "hi"}.get(mode)
        if mode == "auto":
            # Clearly English speech is decoded as English. Everything else (Hindi, Hinglish, Indian English
            # with Hindi words) is decoded Hindi-steered: Hindi comes back in Devanagari and English words stay
            # in Latin letters ("North Zone में secondary sales"), which hindi.normalize reads. On the test
            # clips this beat plain auto-detection (0.5% vs 5.4% word errors).
            detected, prob = self.detect(audio)
            lang = "en" if detected == "en" and prob >= 0.8 else "hi"
        prompt = build_prompt(company, names, lang or "auto")
        res = self.run(audio, lang, prompt, hot)
        if _ARABIC.search(res["text"]):
            # Whisper sometimes writes Hindi in Urdu script: ask again steered to Devanagari
            res = self.run(audio, "hi", build_prompt(company, names, "hi"), hot)
            if _ARABIC.search(res["text"]):
                res = self.run(audio, None, build_prompt(company, names, "auto"), hot)
        res["text"] = snap_names(res["text"], names + [company, "Cooper"])
        return res

    async def transcribe(self, data: bytes, mode: str, company: str, names: list[str]) -> dict:
        audio = decode(data)
        if len(audio) / SAMPLE_RATE > MAX_SECONDS + 1:
            raise ValueError(f"Recording is longer than {MAX_SECONDS} seconds")
        if len(audio) < SAMPLE_RATE // 4:
            return {"text": "", "language": "", "audio_seconds": round(len(audio) / SAMPLE_RATE, 2), "seconds": 0.0,
                    "dropped_segments": 0}
        return await asyncio.to_thread(self.transcribe_sync, audio, mode, company, names)


engine = Engine()
