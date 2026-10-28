import { useCallback, useEffect, useRef, useState } from "react";

/* Records a voice question and sends it to Cooper's own speech model (local Whisper on this laptop).
   - echo cancellation, noise suppression and auto gain on the microphone;
   - a live level meter; the end of speech is detected locally: 2.5 s of silence after you start talking
     (or 30 s max, or nothing heard for 8 s);
   - then the clip is transcribed on the laptop, so the audio never leaves it. */

export const SILENCE_MS = 2500;
const MAX_MS = 30000;
const NO_SPEECH_MS = 8000;

export type VoiceMode = "auto" | "en" | "hi";
export type RecState = "idle" | "recording" | "transcribing";

function pickMime(): string {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  for (const m of options) if ((window as any).MediaRecorder?.isTypeSupported?.(m)) return m;
  return "";
}

export function useRecorder({
  token,
  mode,
  onText,
  onError,
}: {
  token: string;
  mode: VoiceMode;
  onText: (text: string) => void;
  onError: (msg: string) => void;
}) {
  const [state, setState] = useState<RecState>("idle");
  const [level, setLevel] = useState(0); // 0..1 for the meter
  const [silenceFor, setSilenceFor] = useState(0); // ms of silence after speech (drives the countdown bar)
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const ctx = useRef<AudioContext | null>(null);
  const timer = useRef<number | null>(null);
  const chunks = useRef<Blob[]>([]);
  const cancelled = useRef(false);
  const cb = useRef({ onText, onError, token, mode });
  cb.current = { onText, onError, token, mode };
  const supported = typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia && !!(window as any).MediaRecorder;

  const cleanup = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    ctx.current?.close().catch(() => undefined);
    ctx.current = null;
    setLevel(0);
    setSilenceFor(0);
  };

  async function send(blob: Blob) {
    setState("transcribing");
    try {
      const r = await fetch(`/api/transcribe?mode=${cb.current.mode}`, {
        method: "POST",
        headers: { "Content-Type": blob.type.split(";")[0] || "audio/webm", "X-Company-Token": cb.current.token },
        body: blob,
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || "Couldn't transcribe that.");
      const text = String(body.text || "").trim();
      if (!text) cb.current.onError("I didn't catch that. Please try again, a little closer to the mic.");
      else cb.current.onText(text);
    } catch (e) {
      cb.current.onError((e as Error).message);
    } finally {
      setState("idle");
    }
  }

  const start = useCallback(async () => {
    if (state !== "idle") return;
    cancelled.current = false;
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
      stream.current = s;
      const mime = pickMime();
      const rec = new MediaRecorder(s, mime ? { mimeType: mime, audioBitsPerSecond: 48000 } : undefined);
      recorder.current = rec;
      chunks.current = [];
      rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      rec.onstop = () => {
        const blob = new Blob(chunks.current, { type: rec.mimeType || mime || "audio/webm" });
        cleanup();
        if (cancelled.current) setState("idle");
        else void send(blob);
      };
      // Level meter and end-of-speech detection
      const ac = new AudioContext();
      ctx.current = ac;
      const src = ac.createMediaStreamSource(s);
      const an = ac.createAnalyser();
      an.fftSize = 1024;
      src.connect(an);
      const buf = new Float32Array(an.fftSize);
      const t0 = performance.now();
      let floor = 0.01;
      let spoke = false;
      let quietSince = 0;
      timer.current = window.setInterval(() => {
        an.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        const rms = Math.sqrt(sum / buf.length);
        const now = performance.now();
        if (now - t0 < 300) floor = Math.max(0.004, (floor + rms) / 2); // room noise in the first moment
        const threshold = Math.max(0.015, floor * 2.5);
        setLevel(Math.min(1, rms / 0.15));
        if (rms > threshold) {
          spoke = true;
          quietSince = 0;
          setSilenceFor(0);
        } else if (spoke) {
          quietSince = quietSince || now;
          setSilenceFor(now - quietSince);
        }
        const done = (spoke && quietSince && now - quietSince >= SILENCE_MS) || now - t0 >= MAX_MS;
        if (!spoke && now - t0 >= NO_SPEECH_MS) {
          cancelled.current = true;
          cb.current.onError("I didn't hear anything. Click the mic and try again.");
          rec.stop();
        } else if (done && rec.state === "recording") {
          rec.stop();
        }
      }, 50);
      rec.start(250);
      setState("recording");
    } catch (e) {
      cleanup();
      setState("idle");
      const name = (e as Error).name;
      cb.current.onError(
        name === "NotAllowedError"
          ? "Microphone access is blocked. Allow it in the browser's site settings."
          : name === "NotFoundError"
            ? "No microphone was found."
            : "The microphone couldn't start.",
      );
    }
  }, [state]);

  /** Stop now and transcribe what was said so far. */
  const finish = useCallback(() => {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }, []);

  /** Stop and throw the recording away. */
  const cancel = useCallback(() => {
    cancelled.current = true;
    if (recorder.current?.state === "recording") recorder.current.stop();
    else cleanup();
  }, []);

  useEffect(() => () => cancel(), [cancel]);

  return { supported, state, level, silenceFor, start, finish, cancel };
}
