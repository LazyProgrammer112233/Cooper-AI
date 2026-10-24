import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Vocab } from "../api";

/* Voice input through the browser's Web Speech API (Chrome, Edge, Safari). Nothing runs where it isn't
   supported: `supported` is false and the mic button stays hidden.

   Accuracy for Indian English and Hindi:
   - continuous listening, so a short pause mid-sentence doesn't cut the question off;
   - up to 5 alternatives per phrase, re-ranked towards FMCG terms, Hindi words and this company's names;
   - common mishearings fixed (productive cause -> productive calls, you be oh -> UBO, ...), from the server's list;
   - single words close to a zone / state / brand name (one or two letters off) snapped to that name. */

export type VoiceLang = "en-IN" | "hi-IN";
export const SILENCE_MS = 2500;

type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: any) => void) | null;
  onerror: ((e: any) => void) | null;
  onend: (() => void) | null;
};

function ctor(): (new () => Recognition) | null {
  const w = window as any;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

const COMMON = new Set(
  ("the a an and or of to in on for by with from at is are was were be been this that these those what which who why how " +
    "show tell give me my our your list all top bottom best worst highest lowest low high last next week month year today " +
    "yesterday trend compare versus sales sale primary secondary productive calls call total value order orders zone zones " +
    "region regions state states city cities brand brands category categories distributor distributors outlet outlets rep " +
    "reps team coverage penetration drop size growth number count much many per day days quarter wise data report why " +
    "please can you could would should doing wrong improve reason down up more less than between first second third")
    .split(" "),
);

function lev(a: string, b: string, max: number): number {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      best = Math.min(best, cur[j]);
    }
    if (best > max) return max + 1;
    prev = cur;
  }
  return prev[b.length];
}

export function makeCorrector(vocab: Vocab | null) {
  const fixes = (vocab?.fixes ?? []).map(([p, r]) => {
    try {
      return [new RegExp(p, "gi"), r] as const;
    } catch {
      return null;
    }
  }).filter(Boolean) as (readonly [RegExp, string])[];
  const known = new Set<string>([...COMMON]);
  for (const t of [...(vocab?.terms ?? []), ...(vocab?.hindi ?? [])]) t.toLowerCase().split(/\s+/).forEach((w) => known.add(w));
  const names = (vocab?.names ?? []).filter((n) => n.length >= 4);
  const nameWords = new Map<string, string>();
  for (const n of names) {
    known.add(n.toLowerCase());
    if (!n.includes(" ")) nameWords.set(n.toLowerCase(), n);
  }
  const vocabSet = new Set<string>([...known].filter((w) => !COMMON.has(w)));

  function snap(word: string): string {
    const w = word.toLowerCase();
    if (w.length < 4 || known.has(w) || /\d/.test(w)) return word;
    const max = w.length >= 7 ? 2 : 1;
    let best: string | null = null;
    let bestD = max + 1;
    for (const [k, v] of nameWords) {
      const d = lev(w, k, max);
      if (d < bestD) {
        bestD = d;
        best = v;
      }
    }
    return best && bestD <= max ? best : word;
  }

  function correct(text: string): string {
    let t = text;
    for (const [rx, rep] of fixes) t = t.replace(rx, rep);
    return t.replace(/[A-Za-z]{4,}/g, snap).replace(/\s+/g, " ").trim();
  }

  /** How well a transcript matches FMCG and company words (used to pick among alternatives). */
  function score(text: string, confidence: number): number {
    const words = correct(text).toLowerCase().split(/\s+/);
    let hits = 0;
    for (const w of words) if (vocabSet.has(w)) hits++;
    for (const n of names) if (n.includes(" ") && text.toLowerCase().includes(n.toLowerCase())) hits += 2;
    return (confidence || 0) + 0.2 * hits;
  }
  return { correct, score };
}

export function useSpeech({
  lang,
  vocab,
  onText,
  onSilence,
  onDone,
}: {
  lang: VoiceLang;
  vocab: Vocab | null;
  onText: (text: string) => void;
  onSilence: (waiting: boolean) => void; // the 2.5 s auto-send countdown started (true) or was reset (false)
  onDone: (text: string) => void;
}) {
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const committed = useRef(""); // finished phrases from earlier sessions (the browser restarts now and then)
  const finals = useRef<Record<number, string>>({});
  const interim = useRef("");
  const stopping = useRef(false); // we ended it: silence or the user clicked stop
  const send = useRef(false); // send when it ends
  const silence = useRef<number | null>(null);
  const cb = useRef({ onText, onSilence, onDone });
  cb.current = { onText, onSilence, onDone };
  const corrector = useMemo(() => makeCorrector(vocab), [vocab]);
  const supported = typeof window !== "undefined" && !!ctor();

  const text = () =>
    [committed.current, ...Object.keys(finals.current).sort((a, b) => +a - +b).map((k) => finals.current[+k]), interim.current]
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();

  const clearSilence = () => {
    if (silence.current) window.clearTimeout(silence.current);
    silence.current = null;
  };

  const begin = useCallback(
    (r: Recognition) => {
      try {
        r.start();
        return true;
      } catch {
        return false;
      }
    },
    [],
  );

  const start = useCallback(() => {
    const C = ctor();
    if (!C || rec.current) return;
    const r = new C();
    r.lang = lang;
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 5;
    committed.current = "";
    finals.current = {};
    interim.current = "";
    stopping.current = false;
    send.current = false;
    setError(null);
    r.onresult = (e: any) => {
      let live = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) {
          let best = res[0];
          let bestScore = -1;
          for (let a = 0; a < res.length; a++) {
            const s = corrector.score(res[a].transcript, res[a].confidence);
            if (s > bestScore) {
              bestScore = s;
              best = res[a];
            }
          }
          finals.current[i] = corrector.correct(best.transcript);
        } else {
          live += res[0].transcript;
        }
      }
      interim.current = live;
      cb.current.onText(text());
      // Restart the auto-send countdown on every word; it fires after 2.5 s of silence.
      clearSilence();
      cb.current.onSilence(false);
      if (text()) {
        silence.current = window.setTimeout(() => {
          send.current = true;
          stopping.current = true;
          r.stop();
        }, SILENCE_MS);
        cb.current.onSilence(true);
      }
    };
    r.onerror = (e: any) => {
      const map: Record<string, string> = {
        "not-allowed": "Microphone access is blocked. Allow it in the browser's site settings.",
        "service-not-allowed": "Microphone access is blocked. Allow it in the browser's site settings.",
        "no-speech": "I didn't hear anything. Click the mic and try again.",
        network: "The browser's speech service couldn't be reached.",
        "audio-capture": "No microphone was found.",
        "language-not-supported": "This browser can't transcribe that language.",
      };
      if (e.error === "no-speech" && text()) return; // a pause after speaking: keep going
      if (e.error !== "aborted") {
        stopping.current = true;
        send.current = false;
        setError(map[e.error] || "Voice input stopped.");
      }
    };
    r.onend = () => {
      // Chrome ends continuous sessions on its own every so often: carry on unless we stopped it.
      if (!stopping.current) {
        committed.current = text(); // keep everything heard so far, including the last partial phrase
        finals.current = {};
        interim.current = "";
        if (begin(r)) return;
      }
      clearSilence();
      cb.current.onSilence(false);
      rec.current = null;
      setListening(false);
      const final = corrector.correct(text());
      cb.current.onText(final);
      if (send.current && final) cb.current.onDone(final);
    };
    rec.current = r;
    if (begin(r)) setListening(true);
    else {
      rec.current = null;
      setError("Voice input couldn't start.");
    }
  }, [lang, corrector, begin]);

  /** Stop listening and keep the text, without sending. */
  const stop = useCallback(() => {
    clearSilence();
    send.current = false;
    stopping.current = true;
    rec.current?.stop();
  }, []);

  useEffect(
    () => () => {
      clearSilence();
      stopping.current = true;
      rec.current?.abort();
    },
    [],
  );

  return { supported, listening, error, start, stop, clearError: () => setError(null) };
}
