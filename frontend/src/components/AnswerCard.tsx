import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Check, ChevronDown, Info, Shield, Sigma, Sparkles, Table2, BarChart3 } from "lucide-react";
import { useEffect, useState } from "react";
import type { Answer, StageName } from "../api";
import { ChartView, DataTable } from "./ChartView";
import { RcaView } from "./RcaView";

export interface Turn {
  id: string;
  question: string;
  started: number;
  stages: Partial<Record<StageName, { status: "start" | "done"; detail?: string }>>;
  answer?: Answer;
  error?: string;
}

const STEPS: { id: StageName; label: string }[] = [
  { id: "interpret", label: "Understanding" },
  { id: "query", label: "Querying Pulse" },
  { id: "answer", label: "Writing answer" },
];

function Progress({ turn }: { turn: Turn }) {
  const done = !!turn.answer || !!turn.error;
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (done) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [done]);
  const secs = done ? turn.answer?.timings?.total ?? (now - turn.started) / 1000 : (now - turn.started) / 1000;
  return (
    <div className={`progress ${done ? "is-done" : ""}`}>
      {STEPS.map((s) => {
        const st = turn.stages[s.id];
        const state = st?.status === "done" ? "done" : st?.status === "start" ? "active" : done ? "skip" : "todo";
        return (
          <div key={s.id} className={`step step-${state}`} title={st?.detail}>
            <span className="step-dot">{state === "done" ? <Check size={11} strokeWidth={3} /> : null}</span>
            <span className="step-label">{s.label}</span>
          </div>
        );
      })}
      <span className="secs">{secs.toFixed(1)}s</span>
    </div>
  );
}

function FormulaChoice({
  note,
  onChoose,
}: {
  note: Answer["formula_notes"][number];
  onChoose: (metric: string, variant: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <div className={`formula ${note.saved ? "formula-saved" : ""}`}>
      <div className="formula-head">
        <Sigma size={15} />
        <p>{note.message}</p>
      </div>
      {!note.saved && (
        <div className="formula-options">
          {note.options.map((o, i) => (
            <motion.button
              key={o.id}
              className={`formula-opt ${o.id === note.used.id ? "is-used" : ""}`}
              whileHover={{ y: -2 }}
              whileTap={{ scale: 0.98 }}
              disabled={!!busy}
              onClick={async () => {
                setBusy(o.id);
                try {
                  await onChoose(note.metric, o.id);
                } finally {
                  setBusy(null);
                }
              }}
            >
              <span className="opt-num">{i + 1}</span>
              <span className="opt-body">
                <b>
                  {o.label}
                  {o.id === note.used.id && <em> · used now</em>}
                </b>
                <span>{o.formula}</span>
              </span>
              {busy === o.id && <span className="spinner small" />}
            </motion.button>
          ))}
        </div>
      )}
    </div>
  );
}

export function AnswerCard({
  turn,
  onAsk,
  onChooseFormula,
}: {
  turn: Turn;
  onAsk: (q: string) => void;
  onChooseFormula: (metric: string, variant: string, question: string) => Promise<void>;
}) {
  const a = turn.answer;
  const [view, setView] = useState<"chart" | "table">("chart");
  const [howOpen, setHowOpen] = useState(false);
  const chart = a?.chart;
  const canToggle = chart && chart.type !== "table" && a?.table;

  return (
    <div className="turn">
      <motion.div className="bubble-user" initial={{ opacity: 0, x: 16 }} animate={{ opacity: 1, x: 0 }}>
        {turn.question}
      </motion.div>
      <motion.div
        className={`answer answer-${a?.kind ?? "pending"}`}
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 200, damping: 24 }}
      >
        <Progress turn={turn} />
        {!a && !turn.error && (
          <div className="skeleton">
            <div />
            <div />
            <div />
          </div>
        )}
        {turn.error && (
          <div className="answer-alert">
            <AlertTriangle size={16} /> {turn.error}
          </div>
        )}
        <AnimatePresence>
          {a && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.4 }}>
              {a.kind === "refusal" && (
                <div className="answer-alert refusal">
                  <Shield size={16} /> Guardrail
                </div>
              )}
              <p className="summary">
                {a.summary.split(/(\*\*[^*]+\*\*)/).map((part, i) =>
                  part.startsWith("**") ? <b key={i}>{part.slice(2, -2)}</b> : <span key={i}>{part}</span>,
                )}
              </p>
              {a.summary_hi && (
                <p className="summary-hi" lang={a.lang === "hi" ? "hi" : "hi-Latn"}>
                  {a.summary_hi}
                </p>
              )}
              {a.summary_by === "llm" && (
                <div className="byline">
                  <Sparkles size={12} /> Worded by the local model; every number was checked against the data.
                </div>
              )}

              {a.rca && <RcaView rca={a.rca} />}

              {chart && !a.rca && (
                <div className="viz">
                  {canToggle && (
                    <div className="viz-toggle" role="tablist">
                      <button className={view === "chart" ? "on" : ""} onClick={() => setView("chart")} aria-label="Chart view">
                        <BarChart3 size={14} />
                      </button>
                      <button className={view === "table" ? "on" : ""} onClick={() => setView("table")} aria-label="Table view">
                        <Table2 size={14} />
                      </button>
                    </div>
                  )}
                  {chart.type === "table" || view === "table" ? <DataTable table={a.table!} /> : <ChartView chart={chart} />}
                </div>
              )}

              {a.sections?.map((sec, i) => (
                <section key={i} className="answer-section">
                  <h4>
                    <span className="sec-num">{i + 1}</span> {sec.title}
                  </h4>
                  <p className="summary small">{sec.summary}</p>
                  {(sec.chart || sec.table) && (
                    <div className="viz">
                      {sec.chart && sec.chart.type !== "table" ? <ChartView chart={sec.chart} /> : sec.table && <DataTable table={sec.table} />}
                    </div>
                  )}
                </section>
              ))}

              {a.formula_notes.map((n) => (
                <FormulaChoice key={n.metric} note={n} onChoose={(m, v) => onChooseFormula(m, v, a.question)} />
              ))}

              {a.notes.length > 0 && (
                <ul className="notes">
                  {a.notes.map((n, i) => (
                    <li key={i}>
                      <Info size={13} /> {n}
                    </li>
                  ))}
                </ul>
              )}

              {a.terms.length > 0 && (
                <div className="terms">
                  {a.terms.map((t) => (
                    <span key={t.term} className="term">
                      <b>{t.term}</b> {t.full_form}
                    </span>
                  ))}
                </div>
              )}

              {a.how && (
                <div className="how">
                  <button className="how-toggle" onClick={() => setHowOpen((o) => !o)} aria-expanded={howOpen}>
                    How I got this
                    <motion.span animate={{ rotate: howOpen ? 180 : 0 }}>
                      <ChevronDown size={14} />
                    </motion.span>
                  </button>
                  <AnimatePresence initial={false}>
                    {howOpen && (
                      <motion.div
                        className="how-body"
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: "auto", opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                      >
                        <dl>
                          {a.how.constraints && a.how.constraints.length > 0 && (
                            <>
                              <dt>Understood</dt>
                              <dd>
                                <ul className="constraints">
                                  {a.how.constraints.map((c, i) => (
                                    <li key={i}>{c}</li>
                                  ))}
                                </ul>
                              </dd>
                            </>
                          )}
                          {a.how.understood_as && (
                            <>
                              <dt>Understood as</dt>
                              <dd>{a.how.understood_as}</dd>
                            </>
                          )}
                          <dt>Period</dt>
                          <dd>{a.how.period}</dd>
                          <dt>Filters</dt>
                          <dd>{a.how.filters}</dd>
                          {a.how.metrics.map((m) => (
                            <div key={m.label} className="how-metric">
                              <dt>{m.label}</dt>
                              <dd>
                                {m.formula}
                                <small>Source: {m.source}</small>
                              </dd>
                            </div>
                          ))}
                          <dt>Timing</dt>
                          <dd>
                            Understood in {a.timings?.interpret ?? 0}s ({a.how.interpreted_by}), {a.how.queries} Pulse quer
                            {a.how.queries === 1 ? "y" : "ies"} in {a.timings?.query ?? 0}s, answer in {a.timings?.answer ?? 0}s
                          </dd>
                        </dl>
                        <details>
                          <summary>SQL sent to Pulse MCP</summary>
                          {a.how.sql.map((s, i) => (
                            <pre key={i}>{s}</pre>
                          ))}
                        </details>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              )}

              {a.suggestions.length > 0 && (
                <div className="followups">
                  {a.suggestions.map((s) => (
                    <motion.button key={s} className="chip small" whileHover={{ y: -2 }} onClick={() => onAsk(s)}>
                      {s}
                    </motion.button>
                  ))}
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}
