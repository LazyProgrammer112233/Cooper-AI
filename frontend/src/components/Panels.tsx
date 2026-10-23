import { motion } from "framer-motion";
import { RotateCcw, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { getFormulas, getGlossary, setFormula, type FormulaSetting, type GlossaryTerm } from "../api";

export function FormulasPanel({ token, companyName }: { token: string; companyName: string }) {
  const [items, setItems] = useState<FormulaSetting[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => getFormulas(token).then(setItems).catch((e) => setError(e.message));
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
