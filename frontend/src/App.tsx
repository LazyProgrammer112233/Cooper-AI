import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowUp,
  BookOpen,
  Building2,
  LogOut,
  MessageSquare,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Sigma,
  Sun,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ask, getHealth, loadSession, saveSession, setFormula, type Company } from "./api";
import { AnswerCard, type Turn } from "./components/AnswerCard";
import { Gate } from "./components/Gate";
import { Orb } from "./components/Orb";
import { FormulasPanel, GlossaryPanel } from "./components/Panels";
import { greeting } from "./format";

type Tab = "chat" | "formulas" | "glossary";

const CHIPS = [
  "Show primary sales for MTD",
  "Show TC vs PC trend",
  "Which zone has the lowest coverage?",
  "Top 10 brands by secondary sales",
  "Show UBO for last month",
  "What is penetration?",
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

function Composer({ onSend, busy, big }: { onSend: (q: string) => void; busy: boolean; big?: boolean }) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  function send() {
    const q = text.trim();
    if (!q || busy) return;
    onSend(q);
    setText("");
  }
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [text]);
  return (
    <motion.div layout className={`composer ${big ? "composer-big" : ""}`}>
      <textarea
        ref={ref}
        rows={1}
        value={text}
        maxLength={500}
        placeholder="Ask about sales, calls, coverage, UBO..."
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send();
          }
        }}
      />
      <div className="composer-row">
        <span className="hint">Enter to send · Shift+Enter for a new line</span>
        <motion.button
          className="send"
          whileHover={{ scale: 1.06 }}
          whileTap={{ scale: 0.94 }}
          disabled={!text.trim() || busy}
          onClick={send}
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
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getHealth()
      .then((h) => setLlmOk(h.llm.ok))
      .catch(() => setLlmOk(null));
  }, []);

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
        else update((t) => ({ ...t, answer: ev.answer }));
      });
    } catch (err) {
      const msg = (err as Error).message;
      if (/company|session|signature/i.test(msg)) unlock();
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
              <Composer onSend={send} busy={busy} big={empty} />
              <p className="disclaimer">Cooper answers only from {company.name}'s Pulse data. Check important numbers in Pulse.</p>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
