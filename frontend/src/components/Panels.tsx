import { motion } from "framer-motion";
import { RotateCcw, Search, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  correctLearned,
  forgetLearned,
  getFormulas,
  getGlossary,
  getLearned,
  setFormula,
  type FormulaSetting,
  type GlossaryTerm,
  type Learned,
} from "../api";

function LearnedCard({ item, token, onChange }: { item: Learned; token: string; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function pick(i: number) {
    await correctLearned(token, item.slug, { index: i });
    onChange();
  }
  async function saveEdit() {
    setError(null);
    try {
      await correctLearned(token, item.slug, { formula: JSON.parse(text) });
      setEditing(false);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="formula-card learned-card">
      <div className="formula-card-head">
        <h3>
          <Sparkles size={13} /> {item.term}
        </h3>
        <button
          className="link"
          onClick={async () => {
            await forgetLearned(token, item.slug);
            onChange();
          }}
        >
          <Trash2 size={12} /> Forget
        </button>
      </div>
      {item.definition && <p className="small">{item.definition}</p>}
      <p className="muted small">Source: {item.source}</p>
      {item.alternatives.length > 0 ? (
        <>
          <div className="side-label">Correct this formula</div>
          {item.alternatives.map((o, j) => (
            <label key={j} className={`radio ${item.chosen === j ? "on" : ""}`}>
              <input type="radio" name={item.slug} checked={item.chosen === j} onChange={() => pick(j)} />
              <span>
                <b>{o.label}</b>
                <small>{o.text}</small>
              </span>
            </label>
          ))}
        </>
      ) : (
        <p className="small">Pulse doesn't have the data to calculate this for your company yet.</p>
      )}
      <button className="link" onClick={() => setEditing((e) => !e)}>
        {editing ? "Cancel" : "Edit the formula"}
      </button>
      {editing && (
        <div className="learned-edit">
          <textarea
            rows={5}
            value={text}
            placeholder={'{"label": "...", "text": "...", "num": {"agg": "sum", "source": "sds_invoice", "column": "InvoiceQtyInUnit"}, "den": {"agg": "sum", "source": "sds_order", "column": "OrderQtyInUnit"}, "scale": 100}'}
            onChange={(e) => setText(e.target.value)}
          />
          <button className="chip small" onClick={saveEdit}>
            Save formula
          </button>
          {error && <p className="gate-error">{error}</p>}
        </div>
      )}
    </div>
  );
}

export function FormulasPanel({ token, companyName }: { token: string; companyName: string }) {
  const [items, setItems] = useState<FormulaSetting[] | null>(null);
  const [learnedItems, setLearned] = useState<Learned[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    getLearned(token).then(setLearned).catch(() => setLearned([]));
    return getFormulas(token).then(setItems).catch((e) => setError(e.message));
  };
  useEffect(() => {
    load();
  }, [token]);

  async function choose(metric: string, variant: string | null) {
    await setFormula(token, metric, variant);
    await load();
  }

  return (
    <div className="panel">
      <h2>Formulas for {companyName}</h2>
      <p className="panel-sub">
        Cooper uses the basic formula unless you choose another one. Your choice is used for every later answer for {companyName}.
      </p>
      {error && <p className="gate-error">{error}</p>}
      {learnedItems.length > 0 && (
        <>
          <h3 className="panel-h3">Learned metrics</h3>
          <p className="panel-sub">Metrics Cooper looked up in the FieldAssist docs for {companyName}. Pick another formula, edit it, or forget it.</p>
          <div className="formula-grid">
            {learnedItems.map((l) => (
              <LearnedCard key={l.slug} item={l} token={token} onChange={load} />
            ))}
          </div>
          <h3 className="panel-h3">Catalog metrics</h3>
        </>
      )}
      {!items && !error && <div className="skeleton"><div /><div /></div>}
      <div className="formula-grid">
        {items?.map((f, i) => (
          <motion.div
            key={f.metric}
            className="formula-card"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.04 }}
          >
            <div className="formula-card-head">
              <h3>{f.label}</h3>
              {f.saved && (
                <button className="link" onClick={() => choose(f.metric, null)}>
                  <RotateCcw size={12} /> Basic
                </button>
              )}
            </div>
            {f.options.map((o, j) => (
              <label key={o.id} className={`radio ${f.chosen === o.id ? "on" : ""}`}>
                <input type="radio" name={f.metric} checked={f.chosen === o.id} onChange={() => choose(f.metric, o.id)} />
                <span>
                  <b>
                    {o.label}
                    {j === 0 && <em> · basic</em>}
                  </b>
                  <small>{o.formula}</small>
                </span>
              </label>
            ))}
          </motion.div>
        ))}
      </div>
    </div>
  );
}

export function GlossaryPanel() {
  const [terms, setTerms] = useState<GlossaryTerm[]>([]);
  const [q, setQ] = useState("");
  useEffect(() => {
    getGlossary().then(setTerms).catch(() => setTerms([]));
  }, []);
  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return terms;
    return terms.filter((t) =>
      [t.term, t.full_form, t.definition, ...(t.aliases || [])].some((x) => x?.toLowerCase().includes(s)),
    );
  }, [q, terms]);
  return (
    <div className="panel">
      <h2>FMCG glossary</h2>
      <p className="panel-sub">Cooper checks every question against these terms, short forms and slang.</p>
      <div className="search">
        <Search size={15} />
        <input placeholder="Search UBO, PJP, drop size..." value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="gloss-list">
        {shown.map((t) => (
          <div key={t.term} className="gloss">
            <div className="gloss-head">
              <b>{t.term}</b>
              <span>{t.full_form}</span>
              <em>{t.category}</em>
            </div>
            <p>{t.definition}</p>
            {t.basic_formula && (
              <p className="gloss-formula">
                <b>Basic formula:</b> {t.basic_formula}
              </p>
            )}
            {t.aliases?.length > 0 && <p className="gloss-alias">Also said as: {t.aliases.join(", ")}</p>}
          </div>
        ))}
      </div>
    </div>
  );
}
