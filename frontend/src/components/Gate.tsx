import { AnimatePresence, motion } from "framer-motion";
import { ArrowRight, Lock, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { verifyCompany, type Company } from "../api";
import { Orb } from "./Orb";

export function Gate({ onLocked }: { onLocked: (s: { token: string; company: Company }) => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const id = Number(value.trim());
    if (!Number.isInteger(id) || id <= 0) {
      setError("Enter a numeric company ID, for example 10600.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onLocked(await verifyCompany(id));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <div className="bg-mesh" />
      <motion.div
        className="gate-card"
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 160, damping: 20 }}
      >
        <Orb size={84} busy={busy} />
        <h1 className="gate-title">
          Meet <span className="grad-text">Cooper</span>
        </h1>
        <p className="gate-sub">Sales answers from Pulse, for one company at a time.</p>
        <form onSubmit={submit} className="gate-form">
          <label htmlFor="cid" className="sr-only">
            Company ID
          </label>
          <div className={`gate-input ${error ? "has-error" : ""}`}>
            <Lock size={16} />
            <input
              id="cid"
              inputMode="numeric"
              autoFocus
              placeholder="Company ID"
              value={value}
              onChange={(e) => setValue(e.target.value.replace(/[^\d]/g, ""))}
            />
            <motion.button whileHover={{ scale: 1.04 }} whileTap={{ scale: 0.96 }} disabled={busy || !value} type="submit">
              {busy ? <span className="spinner" /> : <ArrowRight size={18} />}
            </motion.button>
          </div>
          <AnimatePresence>
            {error && (
              <motion.p className="gate-error" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
                {error}
              </motion.p>
            )}
          </AnimatePresence>
        </form>
        <div className="gate-foot">
          <ShieldCheck size={14} /> Cooper only ever reads data for the company you lock here.
        </div>
      </motion.div>
    </div>
  );
}
