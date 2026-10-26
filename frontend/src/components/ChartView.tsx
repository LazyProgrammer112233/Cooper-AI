import { motion } from "framer-motion";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Answer, Row, Series } from "../api";
import { fmtTick, fmtValue } from "../format";

// Categorical slots in fixed order (validated palette); slot 1 is the brand blue.
const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];

type Chart = NonNullable<Answer["chart"]>;

function TooltipBox({ active, payload, label, series }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tip">
      <div className="tip-title">{label ?? payload[0]?.payload?.label}</div>
      {payload.map((p: any) => {
        const s = (series as Series[]).find((x) => x.id === p.dataKey) ?? series[0];
        return (
          <div key={p.dataKey} className="tip-row">
            <span className="tip-swatch" style={{ background: p.color || p.payload?.fill }} />
            <span>{s.label}</span>
            <b>{fmtValue(p.value, s.unit)}</b>
          </div>
        );
      })}
    </div>
  );
}

const legendText = (value: string) => <span style={{ color: "var(--text-2)" }}>{value}</span>;

export function Kpi({ chart }: { chart: Chart }) {
  const row = chart.data[0] ?? ({} as Row);
  return (
    <div className="kpis">
      {chart.series.map((s, i) => (
        <motion.div
          key={s.id}
          className="kpi"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.08 * i, type: "spring", stiffness: 220, damping: 22 }}
        >
          <div className="kpi-label">{s.label}</div>
          <div className="kpi-value">{fmtValue(row[s.id], s.unit)}</div>
        </motion.div>
      ))}
    </div>
  );
}

export function DataTable({ table }: { table: NonNullable<Answer["table"]> }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {table.columns.map((c) => (
              <th key={c.key} className={c.unit ? "num" : ""}>
                {c.label || "Item"}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((r, i) => (
            <tr key={i} className={r.match === "✓" ? "is-match" : undefined}>
              {table.columns.map((c) => (
                <td key={c.key} className={c.unit ? "num" : ""}>
                  {c.unit ? fmtValue(r[c.key], c.unit) : String(r[c.key] ?? "–")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChartView({ chart }: { chart: Chart }) {
  const unit = chart.series[0]?.unit;
  const multi = chart.series.length > 1;

  if (chart.type === "kpi") return <Kpi chart={chart} />;

  if (chart.type === "pie") {
    const s = chart.series[0];
    const data = chart.data.filter((r) => Number(r[s.id]) > 0);
    const total = data.reduce((a, r) => a + Number(r[s.id]), 0);
    return (
      <div className="chart chart-pie">
        <ResponsiveContainer width="100%" height={280}>
          <PieChart>
            <Pie
              data={data}
              dataKey={s.id}
              nameKey="label"
              innerRadius="55%"
              outerRadius="85%"
              paddingAngle={1.5}
              stroke="var(--surface)"
              strokeWidth={2}
              animationDuration={900}
              label={({ percent, x, y, textAnchor }) => (
                <text x={x} y={y} textAnchor={textAnchor} dominantBaseline="central" fontSize={12} fontWeight={600} fill="var(--text-2)">
                  {`${(percent * 100).toFixed(0)}%`}
                </text>
              )}
              labelLine={false}
            >
              {data.map((_, i) => (
                <Cell key={i} fill={SERIES[i % SERIES.length]} />
              ))}
            </Pie>
            <Tooltip content={<TooltipBox series={chart.series} />} />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12 }} formatter={legendText} />
          </PieChart>
        </ResponsiveContainer>
        <div className="pie-total">
          <span>Total</span>
          <b>{fmtValue(total, s.unit)}</b>
        </div>
      </div>
    );
  }

  if (chart.type === "hbar") {
    const s = chart.series[0];
    const h = Math.max(160, chart.data.length * 34 + 30);
    return (
      <div className="chart">
        <ResponsiveContainer width="100%" height={h}>
          <BarChart data={chart.data} layout="vertical" margin={{ left: 8, right: 24, top: 4, bottom: 4 }} barCategoryGap={6}>
            <CartesianGrid horizontal={false} stroke="var(--grid)" />
            <XAxis type="number" tickFormatter={(v) => fmtTick(v, s.unit)} tick={{ fontSize: 11, fill: "var(--muted)" }} axisLine={false} tickLine={false} />
            <YAxis type="category" dataKey="label" width={130} tick={{ fontSize: 12, fill: "var(--text-2)" }} axisLine={false} tickLine={false} />
            <Tooltip cursor={{ fill: "var(--hover)" }} content={<TooltipBox series={chart.series} />} />
            <Bar dataKey={s.id} radius={[0, 4, 4, 0]} animationDuration={800} maxBarSize={22}>
              {chart.data.map((r, i) => (
                <Cell key={i} fill={chart.highlight && r.label !== chart.highlight ? "var(--series-1-soft)" : "var(--series-1)"} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    );
  }

  // vertical bars: trends and comparisons (one shared axis; the backend never mixes units here)
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={280}>
        <BarChart data={chart.data} margin={{ left: 0, right: 8, top: 8, bottom: 4 }} barGap={2}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--muted)" }} axisLine={false} tickLine={false} interval="preserveStartEnd" minTickGap={8} />
          <YAxis tickFormatter={(v) => fmtTick(v, unit)} tick={{ fontSize: 11, fill: "var(--muted)" }} axisLine={false} tickLine={false} width={56} />
          <Tooltip cursor={{ fill: "var(--hover)" }} content={<TooltipBox series={chart.series} />} />
          {multi && <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12 }} formatter={legendText} />}
          {chart.series.map((s, i) => (
            <Bar key={s.id} dataKey={s.id} name={s.label} fill={SERIES[i]} radius={[4, 4, 0, 0]} maxBarSize={36} animationDuration={800} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
