import { motion } from "framer-motion";
import { BarChart3, CalendarDays, Lightbulb, Search, Table2, TrendingDown, TrendingUp, Users } from "lucide-react";
import { useState } from "react";
import type { Rca } from "../api";
import { fmtValue } from "../format";
import { ChartView, DataTable } from "./ChartView";

function Section({ icon, title, children, delay = 0 }: { icon: React.ReactNode; title: string; children: React.ReactNode; delay?: number }) {
  return (
    <motion.section className="rca-section" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay }}>
      <h4>
        {icon}
        {title}
      </h4>
      {children}
    </motion.section>
  );
}

export function RcaView({ rca }: { rca: Rca }) {
  const [dim, setDim] = useState(0);
  const [view, setView] = useState<"chart" | "table">("chart");
  const tone = rca.direction === "flat" ? "flat" : rca.worse ? "bad" : "good";
  const block = rca.contributors[dim];
  const cap = (x: string) => x.charAt(0).toUpperCase() + x.slice(1);
  const curName = cap(rca.current.label.split(" (")[0]);
  const prevName = cap(rca.previous.label.split(" (")[0]);
  const chained = rca.drivers.filter((d) => d.impact !== null);
  const other = rca.drivers.filter((d) => d.impact === null);

  return (
    <div className="rca">
      <div className={`rca-head tone-${tone}`}>
        <div className="rca-metric">
          <span>{rca.metric_label}</span>
          <b>{fmtValue(rca.current.value, rca.unit)}</b>
          <small>{rca.current.label}</small>
        </div>
        <div className="rca-change">
          {rca.direction === "up" ? <TrendingUp size={18} /> : <TrendingDown size={18} />}
          <b>{rca.change}</b>
          <small>
            vs {fmtValue(rca.previous.value, rca.unit)} in {rca.previous.label}
          </small>
        </div>
      </div>

      {rca.why.length > 0 && (
        <Section icon={<Search size={15} />} title={rca.direction === "up" ? "Why it went up" : rca.direction === "flat" ? "What's holding it back" : "Why it happened"}>
          <ol className="rca-why">
            {rca.why.map((w, i) => (
              <li key={i}>
                <span className="why-num">{i + 1}</span>
                <span>
                  {w.likely && <em className="likely">Likely</em>}
                  {w.text}
                </span>
              </li>
            ))}
          </ol>
        </Section>
      )}

      {rca.drivers.length > 0 && (
        <Section icon={<BarChart3 size={15} />} title="Driver tree" delay={0.05}>
          {chained.length > 0 && <p className="rca-sub">{chained[0].chain} = {chained.map((d) => d.label).join(" × ")}</p>}
          <div className="table-wrap">
            <table className="rca-drivers">
              <thead>
                <tr>
                  <th>Driver</th>
                  <th className="num">{prevName}</th>
                  <th className="num">{curName}</th>
                  <th className="num">Change</th>
                  <th>Share of change</th>
                </tr>
              </thead>
              <tbody>
                {[...chained, ...other].map((d) => (
                  <tr key={d.label}>
                    <td>{d.label}</td>
                    <td className="num">{fmtValue(d.prev, d.unit)}</td>
                    <td className="num">{fmtValue(d.cur, d.unit)}</td>
                    <td className={`num ${/down|later/.test(d.change) ? "neg" : /up|earlier/.test(d.change) ? "pos" : ""}`}>{d.change}</td>
                    <td>
                      {d.impact !== null ? (
                        <span className="impact">
                          <span className="impact-bar" style={{ width: `${Math.min(100, Math.max(0, d.impact))}%` }} />
                          <span>{d.impact > 0 ? `${d.impact.toFixed(0)}%` : "offset"}</span>
                        </span>
                      ) : (
                        <span className="muted">–</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {rca.calendar.length > 0 && (
        <Section icon={<CalendarDays size={15} />} title="Calendar and season" delay={0.1}>
          <ul className="rca-cal">
            {rca.calendar.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </Section>
      )}

      {block && (
        <Section icon={<TrendingDown size={15} />} title="Where it moved most" delay={0.15}>
          <div className="rca-tabs" role="tablist">
            {rca.contributors.map((c, i) => (
              <button key={c.dim} role="tab" aria-selected={i === dim} className={i === dim ? "on" : ""} onClick={() => setDim(i)}>
                {c.label.replace(/^Biggest (drops|gains) by /, "")}
              </button>
            ))}
            <span className="rca-tabs-gap" />
            <button className={view === "chart" ? "on" : ""} onClick={() => setView("chart")} aria-label="Chart view">
              <BarChart3 size={13} />
            </button>
            <button className={view === "table" ? "on" : ""} onClick={() => setView("table")} aria-label="Table view">
              <Table2 size={13} />
            </button>
          </div>
          <p className="rca-sub">{block.label}</p>
          <div className="viz">
            {view === "chart" ? (
              <ChartView
                chart={{
                  type: "bar",
                  series: [
                    { id: "prev", label: prevName, unit: block.unit },
                    { id: "cur", label: curName, unit: block.unit },
                  ],
                  data: block.rows,
                  highlight: null,
                  x_label: "",
                }}
              />
            ) : (
              <DataTable table={block} />
            )}
          </div>
        </Section>
      )}

      {rca.coach && (
        <Section icon={<Users size={15} />} title={rca.coach.title} delay={0.2}>
          <DataTable table={rca.coach} />
        </Section>
      )}

      {rca.actions.length > 0 && (
        <Section icon={<Lightbulb size={15} />} title="What to do" delay={0.25}>
          <div className="rca-actions">
            {rca.actions.map((a) => (
              <div key={a.title} className="rca-action">
                <b>{a.title}</b>
                <ul>
                  {a.steps.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <p className="rca-foot">Suggestions come from Cooper's FMCG playbook, matched to what the data shows.</p>
        </Section>
      )}
    </div>
  );
}
