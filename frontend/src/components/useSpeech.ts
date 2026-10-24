import { useCallback, useEffect, useRef, useState } from "react";

/* Voice input through the browser's Web Speech API (Chrome, Edge, Safari). Nothing runs where it isn't
   supported: `supported` is false and the mic button stays hidden. */

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

export function useSpeech({ onText, onDone }: { onText: (text: string) => void; onDone: (text: string) => void }) {
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const finalText = useRef("");
  const lastText = useRef("");
  const cancelled = useRef(false);
  const cb = useRef({ onText, onDone });
  cb.current = { onText, onDone };
  const supported = typeof window !== "undefined" && !!ctor();

  const start = useCallback(() => {
    const C = ctor();
    if (!C || listening) return;
    const r = new C();
    r.lang = "en-IN";
    r.continuous = false; // ends by itself after a pause, which is our "stopped talking" signal
    r.interimResults = true;
    r.maxAlternatives = 1;
    finalText.current = "";
    lastText.current = "";
    cancelled.current = false;
    setError(null);
    r.onresult = (e: any) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText.current += t;
        else interim += t;
      }
      lastText.current = (finalText.current + interim).replace(/\s+/g, " ").trim();
      cb.current.onText(lastText.current);
    };
    r.onerror = (e: any) => {
      cancelled.current = true;
      const map: Record<string, string> = {
        "not-allowed": "Microphone access is blocked. Allow it in the browser's site settings.",
        "service-not-allowed": "Microphone access is blocked. Allow it in the browser's site settings.",
        "no-speech": "I didn't hear anything. Click the mic and try again.",
        network: "The browser's speech service couldn't be reached.",
        "audio-capture": "No microphone was found.",
      };
      if (e.error !== "aborted") setError(map[e.error] || "Voice input stopped.");
    };
    r.onend = () => {
      setListening(false);
      rec.current = null;
      if (!cancelled.current && lastText.current) cb.current.onDone(lastText.current);
    };
    rec.current = r;
    try {
      r.start();
      setListening(true);
    } catch {
      setError("Voice input couldn't start.");
    }
  }, [listening]);

  /** Stop listening and keep the text, without sending. */
  const stop = useCallback(() => {
    cancelled.current = true;
    rec.current?.stop();
  }, []);

  useEffect(() => () => rec.current?.abort(), []);

  return { supported, listening, error, start, stop, clearError: () => setError(null) };
}
