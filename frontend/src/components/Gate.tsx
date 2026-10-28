import { AnimatePresence, motion } from "framer-motion";
import { ArrowRight, CheckCircle2, KeyRound, Lock, PlugZap, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { connectMcp, disconnectMcp, getMcpStatus, verifyCompany, type Company, type McpStatus } from "../api";
import { Orb } from "./Orb";

/** Reads ?mcp=connected / ?mcp=error&message=... left by the sign-in redirect, then cleans the URL. */
function takeSignInResult(): { ok: boolean; message?: string } | null {
  const params = new URLSearchParams(window.location.search);
  const mcp = params.get("mcp");
  if (!mcp) return null;
  window.history.replaceState(null, "", window.location.pathname);
  return mcp === "connected" ? { ok: true } : { ok: false, message: params.get("message") || "Sign-in failed." };
}

function ConnectStep({ onConnected, initialError }: { onConnected: () => void; initialError: string | null }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(initialError);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      const r = await connectMcp();
      if (r.auth_url) {
        window.location.href = r.auth_url; // FieldAssist sign-in; it sends the browser back here
        return;
      }
      onConnected();
    } catch (err) {
      setError((err as Error).message);
    }
    setBusy(false);
  }

  return (
    <motion.div key="connect" className="gate-step" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
      <span className="step-tag">Step 1 of 2</span>
      <motion.button
        className="cta"
        onClick={connect}
        disabled={busy}
        whileHover={{ y: -2, scale: 1.01 }}
        whileTap={{ scale: 0.98 }}
      >
        <span className="cta-shine" />
        {busy ? <span className="spinner" /> : <PlugZap size={20} />}
        <span className="cta-text">
          <b>Connect and authorize Pulse MCP</b>
          <small>Sign in with your FieldAssist ID</small>
        </span>
      </motion.button>
      <AnimatePresence>
        {error && (
          <motion.p className="gate-error" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
            {error}
          </motion.p>
        )}
      </AnimatePresence>
      <p className="gate-note">
        <KeyRound size={13} /> Your sign-in stays on this laptop. Cooper only uses Pulse MCP's read-only query tool.
      </p>
    </motion.div>
  );
}

function CompanyStep({ onLocked, mcp, onDisconnect }: {
  onLocked: (s: { token: string; company: Company }) => void;
  mcp: McpStatus;
  onDisconnect: () => void;
}) {
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
    <motion.div key="company" className="gate-step" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
      <div className="connected-chip">
        <CheckCircle2 size={15} /> Pulse MCP connected
        {!mcp.static_token && (
          <button className="link" onClick={onDisconnect}>
            Disconnect
          </button>
        )}
      </div>
      <span className="step-tag">Step 2 of 2</span>
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
          <motion.button whileHover={{ scale: 1.04 }} whileTap={{ scale: 0.96 }} disabled={busy || !value} type="submit" aria-label="Lock company">
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
    </motion.div>
  );
}

export function Gate({ onLocked }: { onLocked: (s: { token: string; company: Company }) => void }) {
  const [mcp, setMcp] = useState<McpStatus | null>(null);
  const [signIn] = useState(takeSignInResult);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = () =>
    getMcpStatus()
      .then(setMcp)
      .catch((e) => setLoadError(`Cooper's server isn't responding (${(e as Error).message}). Is start.sh running?`));
  useEffect(() => {
    refresh();
  }, []);

  const connected = !!mcp && (mcp.connected || mcp.static_token);

  return (
    <div className="gate">
      <div className="bg-mesh" />
      <motion.div
        className="gate-card"
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 160, damping: 20 }}
      >
        <Orb size={84} />
        <h1 className="gate-title">
          Meet <span className="grad-text">Cooper</span>
        </h1>
        <p className="gate-sub">Sales answers from Pulse, for one company at a time.</p>
        {loadError && <p className="gate-error">{loadError}</p>}
        {!mcp && !loadError && <div className="skeleton"><div /><div /></div>}
        <AnimatePresence mode="wait">
          {mcp && !connected && (
            <ConnectStep onConnected={refresh} initialError={signIn && !signIn.ok ? signIn.message ?? null : mcp.error} />
          )}
          {mcp && connected && (
            <CompanyStep mcp={mcp} onLocked={onLocked} onDisconnect={() => disconnectMcp().then(setMcp)} />
          )}
        </AnimatePresence>
        <div className="gate-foot">
          <ShieldCheck size={14} /> Cooper only ever reads data for the company you lock here.
        </div>
      </motion.div>
    </div>
  );
}
