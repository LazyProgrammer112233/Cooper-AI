import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowUp,
  BookOpen,
  Building2,
  LogOut,
  MessageSquare,
  Mic,
  Square,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Sigma,
  Sun,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ask, getHealth, getVocab, loadSession, saveSession, setFormula, type Company, type Vocab } from "./api";
import { AnswerCard, type Turn } from "./components/AnswerCard";
import { Gate } from "./components/Gate";
import { Orb } from "./components/Orb";
import { FormulasPanel, GlossaryPanel } from "./components/Panels";
import { makeCorrector, useSpeech } from "./components/useSpeech";
import { SILENCE_MS, useRecorder, type VoiceMode } from "./components/useRecorder";
import { greeting } from "./format";

type Tab = "chat" | "formulas" | "glossary";

const CHIPS = [
  "Show primary sales for MTD",
  "Show TC vs PC trend",
  "Which zone has the lowest coverage?",
  "Top 10 brands by secondary sales",
  "Show UBO for last month",
  "Why are my productive calls low this week?",
  "Is hafte PC kam kyun hai?",
];


function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try {
      const t = localStorage.getItem("cooper.theme");
      if (t === "light" || t === "dark") return t;
    } catch {
      /* ignore */
    }
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("cooper.theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "light" ? "dark" : "light"))] as const;
}

const REVIEW_MS = 1000; // a voice question shows in the box this long before it sends
const MODES: { id: VoiceMode; label: string; title: string }[] = [
  { id: "auto", label: "Auto", title: "Voice language: automatic (English, Hindi or Hinglish)" },
  { id: "en", label: "EN", title: "Voice language: English (India)" },
  { id: "hi", label: "हिं", title: "Voice language: हिन्दी" },
];

function useVoiceMode() {
  const [mode, setMode] = useState<VoiceMode>(() => {
    try {
      const m = localStorage.getItem("cooper.voiceMode");
      return m === "en" || m === "hi" ? m : "auto";
    } catch {
      return "auto";
    }
  });
  function next() {
    setMode((m) => {
      const n: VoiceMode = m === "auto" ? "en" : m === "en" ? "hi" : "auto";
      try {
        localStorage.setItem("cooper.voiceMode", n);
      } catch {
        /* ignore */
      }
      return n;
    });
  }
  return [mode, next] as const;
}

function Composer({
  onSend,
  busy,
  big,
  vocab,
  token,
  sttReady,
}: {
  onSend: (q: string) => void;
  busy: boolean;
  big?: boolean;
  vocab: Vocab | null;
  token: string;
  sttReady: boolean;
}) {
  const [text, setText] = useState("");
  const [review, setReview] = useState(0); // >0 while a voice question waits to send (restarts the bar)
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [mode, nextMode] = useVoiceMode();
  const ref = useRef<HTMLTextAreaElement>(null);
  const prefix = useRef("");
  const timer = useRef<number | null>(null);
  const corrector = useMemo(() => makeCorrector(vocab), [vocab]);

  function cancelReview() {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
    setReview(0);
  }
  function send(q0?: string) {
    const q = (q0 ?? text).trim();
    cancelReview();
    if (!q || busy) return;
    onSend(q);
    setText("");
  }
  const join = (t: string) => ((prefix.current ? prefix.current + " " : "") + t).trim();
  function heard(t: string, delay: number) {
    const full = join(corrector.correct(t));
    setText(full);
    setReview((n) => n + 1);
    timer.current = window.setTimeout(() => send(full), delay);
  }

  // Cooper's own speech model (local Whisper): record, detect the end of speech, transcribe on the laptop
  const rec = useRecorder({ token, mode, onText: (t) => heard(t, REVIEW_MS), onError: setVoiceError });
  // Fallback while the model loads or if it can't run: the browser's built-in recognition
  const speech = useSpeech({
    lang: mode === "hi" ? "hi-IN" : "en-IN",
    vocab,
    onText: (t) => setText(join(t)),
    onSilence: () => undefined,
    onDone: (t) => heard(t, REVIEW_MS),
  });
  const useWhisper = sttReady && rec.supported;
  const canVoice = useWhisper || speech.supported;
  const recording = useWhisper ? rec.state === "recording" : speech.listening;
  const transcribing = rec.state === "transcribing";

  function toggleMic() {
    setVoiceError(null);
    if (recording) {
      if (useWhisper) rec.finish();
      else speech.stop();
      return;
    }
    cancelReview();
    prefix.current = text.trim();
    if (useWhisper) void rec.start();
    else speech.start();
  }
  function cancelVoice() {
    if (useWhisper) rec.cancel();
    else speech.stop();
    cancelReview();
  }
  useEffect(() => () => cancelReview(), []);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [text]);
  const hi = mode === "hi";
  const modeInfo = MODES.find((m) => m.id === mode)!;
  const engineNote = useWhisper
    ? "Transcribed on this laptop by Cooper's own speech model; the audio never leaves it."
    : "Cooper's speech model isn't ready yet, so the browser's recognition is used (Chrome and Edge send the audio to their speech service).";
  const left = Math.max(0, SILENCE_MS - rec.silenceFor);

  return (
    <motion.div layout className={`composer ${big ? "composer-big" : ""} ${recording ? "is-listening" : ""}`}>
      <textarea
        ref={ref}
        rows={1}
        value={text}
        maxLength={500}
        lang={hi ? "hi" : "en"}
        readOnly={transcribing}
        placeholder={
          recording
            ? hi
              ? "बोलिए... (हिन्दी या Hinglish)"
              : "Listening... speak your question"
            : transcribing
              ? "Transcribing..."
              : "Ask in English, Hindi or Hinglish: sales, calls, coverage, or why a number changed..."
        }
        onChange={(e) => {
          cancelReview(); // typing takes over: no auto-send
          if (recording) cancelVoice();
          setText(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape" && (recording || review)) {
            e.preventDefault();
            cancelVoice();
          } else if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send();
          }
        }}
      />
      <div className="composer-row">
        {review > 0 ? (
          <span className="hint voice-hint">
            <span key={review} className="voice-bar" style={{ animationDuration: `${REVIEW_MS}ms` }} />
            {hi ? "Bhej raha hoon... badliye ya " : "Sending... edit it or "}
            <button className="link-btn" onClick={cancelReview}>
              {hi ? "rokiye" : "cancel"}
            </button>
          </span>
        ) : transcribing ? (
          <span className="hint voice-hint">
            <span className="spinner tiny" /> {hi ? "Samajh raha hoon..." : "Transcribing on this laptop..."}
          </span>
        ) : recording ? (
          <span className="hint voice-hint">
            {useWhisper && (
              <span className="level" aria-hidden>
                {[0.15, 0.35, 0.6, 0.85].map((t) => (
                  <i key={t} className={rec.level > t ? "on" : ""} />
                ))}
              </span>
            )}
            {useWhisper && rec.silenceFor > 400
              ? `${hi ? "Ruk gaye? Bhej raha hoon" : "Stopped talking? Sending in"} ${(left / 1000).toFixed(1)}s · `
              : `${hi ? "सुन रहा हूँ" : "Listening"} (${modeInfo.label}) · `}
            <button className="link-btn" onClick={cancelVoice}>
              {hi ? "रद्द करें" : "cancel"}
            </button>
          </span>
        ) : voiceError || speech.error ? (
          <span className="hint voice-error" onClick={() => { setVoiceError(null); speech.clearError(); }}>
            {voiceError || speech.error}
          </span>
        ) : (
          <span className="hint">Enter to send · Shift+Enter for a new line{canVoice ? " · or use the mic" : ""}</span>
        )}
        {canVoice && (
          <button
            className={`voice-lang mode-${mode}`}
            onClick={nextMode}
            disabled={recording || transcribing}
            title={`${modeInfo.title}. Click to switch (Auto → English → हिन्दी).`}
            aria-label={`${modeInfo.title}. Switch voice language`}
          >
            {modeInfo.label}
          </button>
        )}
        {canVoice && (
          <motion.button
            className={`mic ${recording ? "on" : ""} ${useWhisper ? "local" : "browser"}`}
            whileHover={{ scale: 1.06 }}
            whileTap={{ scale: 0.94 }}
            disabled={busy || transcribing}
            onClick={toggleMic}
            style={recording && useWhisper ? ({ "--lvl": rec.level } as React.CSSProperties) : undefined}
            aria-label={recording ? "Stop and send" : "Speak your question"}
            title={recording ? "Stop now and transcribe" : `Speak your question (${modeInfo.title.replace("Voice language: ", "")}). ${engineNote}`}
          >
            {recording && <span className="mic-pulse" />}
            {transcribing ? <span className="spinner small" /> : recording ? <Square size={14} fill="currentColor" /> : <Mic size={18} />}
          </motion.button>
        )}
        <motion.button
          className="send"
          whileHover={{ scale: 1.06 }}
          whileTap={{ scale: 0.94 }}
          disabled={!text.trim() || busy || transcribing}
          onClick={() => send()}
          aria-label="Send"
        >
          {busy ? <span className="spinner" /> : <ArrowUp size={18} />}
        </motion.button>
      </div>
    </motion.div>
  );
}

export default function App() {
  const [session, setSession] = useState(loadSession);
  const [theme, toggleTheme] = useTheme();
  const [tab, setTab] = useState<Tab>("chat");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [sideOpen, setSideOpen] = useState(() => window.innerWidth > 900);
  const [llmOk, setLlmOk] = useState<boolean | null>(null);
  const [vocab, setVocab] = useState<Vocab | null>(null);
  const [sttReady, setSttReady] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // The speech model loads in the background after the server starts: check until it's ready
    let stop = false;
    let tries = 0;
    const check = () =>
      getHealth()
        .then((h) => {
          setLlmOk(h.llm.ok);
          const st = h.stt?.state;
          setSttReady(st === "ready");
          if (!stop && (st === "loading" || st === "off" || !st) && tries++ < 120) window.setTimeout(check, 5000);
        })
        .catch(() => setLlmOk(null));
    check();
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    // Company names and FMCG words the voice input should favour (fetched once per company)
    setVocab(null);
    if (session) getVocab(session.token).then(setVocab).catch(() => setVocab(null));
  }, [session?.token]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  function lock(s: { token: string; company: Company }) {
    saveSession(s);
    setSession(s);
    setTurns([]);
  }

  function unlock() {
    // Changing company always starts a clean chat, so nothing from the last company stays on screen.
    saveSession(null);
    setSession(null);
    setTurns([]);
    setTab("chat");
  }

  async function send(question: string) {
    if (!session || busy) return;
    setTab("chat");
    const id = crypto.randomUUID?.() ?? String(Date.now());
    const turn: Turn = { id, question, started: Date.now(), stages: {} };
    setTurns((t) => [...t, turn]);
    setBusy(true);
    const update = (fn: (t: Turn) => Turn) => setTurns((all) => all.map((x) => (x.id === id ? fn(x) : x)));
    try {
      await ask(question, session.token, (ev) => {
        if (ev.type === "stage") update((t) => ({ ...t, stages: { ...t.stages, [ev.stage]: { status: ev.status, detail: ev.detail } } }));
        else {
          update((t) => ({ ...t, answer: ev.answer }));
          if (ev.answer.kind === "error" && /connect and authorize/i.test(ev.answer.summary)) setTimeout(unlock, 2500);
        }
      });
    } catch (err) {
      const msg = (err as Error).message;
      if (/company|session|signature|connect and authorize/i.test(msg)) unlock();
      update((t) => ({ ...t, error: msg }));
    } finally {
      setBusy(false);
    }
  }

  async function chooseFormula(metric: string, variant: string, question: string) {
    if (!session) return;
    await setFormula(session.token, metric, variant);
    await send(question);
  }

  if (!session) return <Gate onLocked={lock} />;
  const { company } = session;
  const empty = turns.length === 0;

  return (
    <div className={`shell ${sideOpen ? "side-open" : ""}`}>
      <div className="bg-mesh" />
      <AnimatePresence>
        {sideOpen && (
          <motion.aside
            className="sidebar"
            initial={{ x: -280, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: -280, opacity: 0 }}
            transition={{ type: "spring", stiffness: 260, damping: 30 }}
          >
            <div className="brand">
              <Orb size={22} />
              <span>Cooper</span>
              <button className="icon-btn" onClick={() => setSideOpen(false)} aria-label="Close sidebar">
                <PanelLeftClose size={16} />
              </button>
            </div>
            <button className="new-chat" onClick={() => { setTurns([]); setTab("chat"); }}>
              <Plus size={16} /> New chat
            </button>
            <div className="side-label">This chat</div>
            <div className="history">
              {turns.length === 0 && <p className="muted small">Your questions will show up here.</p>}
              {turns.map((t) => (
                <button
                  key={t.id}
                  className="history-item"
                  onClick={() => document.getElementById(`turn-${t.id}`)?.scrollIntoView({ behavior: "smooth" })}
                >
                  <MessageSquare size={13} /> <span>{t.question}</span>
                </button>
              ))}
            </div>
            <div className="company-card">
              <div className="company-icon">
                <Building2 size={16} />
              </div>
              <div className="company-meta">
                <b>{company.name}</b>
                <span>
                  ID {company.id} · data to{" "}
                  {new Date(company.last_data).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}
                </span>
              </div>
              <button className="icon-btn" onClick={unlock} title="Change company" aria-label="Change company">
                <LogOut size={15} />
              </button>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>

      <main className="main">
        <header className="topbar">
          {!sideOpen && (
            <button className="icon-btn" onClick={() => setSideOpen(true)} aria-label="Open sidebar">
              <PanelLeftOpen size={16} />
            </button>
          )}
          <nav className="pills" role="tablist">
            {(
              [
                ["chat", "Chat", MessageSquare],
                ["formulas", "Formulas", Sigma],
                ["glossary", "Glossary", BookOpen],
              ] as const
            ).map(([id, label, Icon]) => (
              <button key={id} role="tab" aria-selected={tab === id} className={`pill ${tab === id ? "on" : ""}`} onClick={() => setTab(id)}>
                {tab === id && <motion.span layoutId="pill-bg" className="pill-bg" transition={{ type: "spring", stiffness: 380, damping: 32 }} />}
                <Icon size={14} />
                <span>{label}</span>
              </button>
            ))}
          </nav>
          <div className="topbar-right">
            <span className="company-pill" title="Locked company">
              <span className="dot" /> {company.name} · {company.id}
            </span>
            <button className="icon-btn" onClick={toggleTheme} aria-label="Toggle dark mode">
              {theme === "light" ? <Moon size={16} /> : <Sun size={16} />}
            </button>
          </div>
        </header>

        {tab === "formulas" && <FormulasPanel token={session.token} companyName={company.name} />}
        {tab === "glossary" && <GlossaryPanel />}
        {tab === "chat" && (
          <>
            <div className="scroll">
              <AnimatePresence mode="wait">
                {empty ? (
                  <motion.section
                    key="home"
                    className="home"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0, y: -20 }}
                  >
                    <Orb size={110} busy={busy} />
                    <motion.h1 initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }}>
                      {greeting()}
                    </motion.h1>
                    <motion.p className="home-sub" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.18 }}>
                      What would you like to know about <span className="grad-text">{company.name}</span>?
                    </motion.p>
                    <div className="chips">
                      {CHIPS.map((c, i) => (
                        <motion.button
                          key={c}
                          className="chip"
                          initial={{ opacity: 0, y: 10 }}
                          animate={{ opacity: 1, y: 0 }}
                          transition={{ delay: 0.25 + i * 0.05 }}
                          whileHover={{ y: -3 }}
                          onClick={() => send(c)}
                        >
                          {c}
                        </motion.button>
                      ))}
                    </div>
                    {llmOk === false && (
                      <p className="muted small">The local model isn't running, so answers use Cooper's built-in wording.</p>
                    )}
                  </motion.section>
                ) : (
                  <motion.section key="chat" className="thread" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                    {turns.map((t) => (
                      <div key={t.id} id={`turn-${t.id}`}>
                        <AnswerCard turn={t} onAsk={send} onChooseFormula={chooseFormula} />
                      </div>
                    ))}
                    <div ref={endRef} />
                  </motion.section>
                )}
              </AnimatePresence>
            </div>
            <div className="dock">
              <Composer onSend={send} busy={busy} big={empty} vocab={vocab} token={session.token} sttReady={sttReady} />
              <p className="disclaimer">Cooper answers only from {company.name}'s Pulse data. Check important numbers in Pulse.</p>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
