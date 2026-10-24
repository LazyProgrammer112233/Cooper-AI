export type Unit = "currency" | "count" | "percent" | "time" | "number";

export interface Company {
  id: number;
  name: string;
  tz_offset: number;
  fy_start_month: number;
  last_data: string;
}

export interface FormulaOption {
  id: string;
  label: string;
  formula: string;
}

export interface FormulaNote {
  metric: string;
  metric_label: string;
  saved: boolean;
  message: string;
  used: FormulaOption;
  options: FormulaOption[];
}

export interface Series {
  id: string;
  label: string;
  unit: Unit;
}

export type Row = Record<string, string | number | null | string[]> & { label: string };

export interface Answer {
  kind: "data" | "empty" | "glossary" | "help" | "clarify" | "refusal" | "error" | "rca";
  question: string;
  summary: string;
  summary_by?: "template" | "llm";
  summary_hi?: string;
  lang?: "en" | "hi" | "hinglish";
  headline?: string | null;
  chart: null | {
    type: "kpi" | "bar" | "hbar" | "pie" | "table";
    series: Series[];
    data: Row[];
    highlight: string | null;
    x_label: string;
  };
  table: null | { columns: { key: string; label: string; unit?: Unit }[]; rows: Row[] };
  formula_notes: FormulaNote[];
  notes: string[];
  terms: { term: string; full_form: string }[];
  suggestions: string[];
  how?: {
    period: string;
    filters: string;
    metrics: { label: string; formula: string; source: string }[];
    interpreted_by: string;
    queries: number;
    sql: string[];
    understood_as?: string;
  };
  timings?: Record<string, number>;
  rca?: Rca;
}

export interface RcaTable {
  columns: { key: string; label: string; unit?: Unit }[];
  rows: Row[];
}

export interface Rca {
  metric: string;
  metric_label: string;
  unit: Unit;
  direction: "up" | "down" | "flat";
  worse: boolean;
  current: { label: string; value: number | null };
  previous: { label: string; value: number | null };
  change: string;
  why: { id: string; text: string; likely: boolean }[];
  drivers: { label: string; unit: Unit; cur: number | null; prev: number | null; change: string; impact: number | null; chain?: string | null }[];
  calendar: string[];
  contributors: (RcaTable & { dim: string; label: string; unit: Unit })[];
  coach: (RcaTable & { title: string }) | null;
  actions: { title: string; steps: string[] }[];
}

export type StageName = "interpret" | "query" | "answer";

export interface StageEvent {
  type: "stage";
  stage: StageName;
  status: "start" | "done";
  detail?: string;
}

export interface GlossaryTerm {
  term: string;
  full_form: string;
  aliases: string[];
  category: string;
  definition: string;
  basic_formula?: string | null;
  metric_id?: string | null;
}

export interface FormulaSetting {
  metric: string;
  label: string;
  chosen: string;
  saved: boolean;
  options: FormulaOption[];
}

const TOKEN_KEY = "cooper.company";

export function loadSession(): { token: string; company: Company } | null {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function saveSession(s: { token: string; company: Company } | null) {
  try {
    if (s) sessionStorage.setItem(TOKEN_KEY, JSON.stringify(s));
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: keep it in memory only */
  }
}

async function errorText(r: Response): Promise<string> {
  try {
    const body = await r.json();
    return body.detail || r.statusText;
  } catch {
    return r.statusText || "Request failed";
  }
}

export async function verifyCompany(companyId: number): Promise<{ token: string; company: Company }> {
  const r = await fetch("/api/company/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ company_id: companyId }),
  });
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}

export async function ask(
  question: string,
  token: string,
  onEvent: (ev: StageEvent | { type: "answer"; answer: Answer }) => void,
  signal?: AbortSignal,
) {
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Company-Token": token },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!r.ok || !r.body) throw new Error(await errorText(r));
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

export async function getFormulas(token: string): Promise<FormulaSetting[]> {
  const r = await fetch("/api/formulas", { headers: { "X-Company-Token": token } });
  if (!r.ok) throw new Error(await errorText(r));
  return (await r.json()).formulas;
}

export async function setFormula(token: string, metric: string, variant: string | null) {
  const r = await fetch("/api/formulas", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Company-Token": token },
    body: JSON.stringify({ metric, variant }),
  });
  if (!r.ok) throw new Error(await errorText(r));
}

export async function getGlossary(): Promise<GlossaryTerm[]> {
  const r = await fetch("/api/glossary");
  if (!r.ok) throw new Error(await errorText(r));
  return (await r.json()).terms;
}

export interface McpStatus {
  connected: boolean;
  pending: boolean;
  server_url: string;
  error: string | null;
  static_token: boolean;
}

export async function getMcpStatus(): Promise<McpStatus> {
  const r = await fetch("/api/mcp/status");
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}

export async function connectMcp(): Promise<{ auth_url?: string; connected?: boolean }> {
  const r = await fetch("/api/mcp/connect", { method: "POST" });
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}

export async function disconnectMcp(): Promise<McpStatus> {
  const r = await fetch("/api/mcp/disconnect", { method: "POST" });
  return r.json();
}

export interface SttStatus {
  enabled: boolean;
  state: "off" | "loading" | "ready" | "error";
  model: string;
  device: string;
  error: string | null;
}

export async function getHealth(): Promise<{ llm: { ok: boolean; model?: string }; mcp: McpStatus; stt?: SttStatus }> {
  const r = await fetch("/api/health");
  return r.json();
}

export interface Vocab {
  names: string[];
  terms: string[];
  hindi: string[];
  fixes: [string, string][];
}

export async function getVocab(token: string): Promise<Vocab> {
  const r = await fetch("/api/vocab", { headers: { "X-Company-Token": token } });
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}
