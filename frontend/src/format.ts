import type { Unit } from "./api";

export function indianInt(n: number): string {
  const neg = n < 0;
  let s = String(Math.round(Math.abs(n)));
  if (s.length > 3) {
    let head = s.slice(0, -3);
    const tail = s.slice(-3);
    const groups: string[] = [];
    while (head.length > 2) {
      groups.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) groups.unshift(head);
    s = groups.join(",") + "," + tail;
  }
  return (neg ? "-" : "") + s;
}

export function money(v: number): string {
  const a = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(2)} L`;
  return `${sign}₹${indianInt(a)}`;
}

export function clock(sec: number): string {
  const s = Math.round(sec) % 86400;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

export function fmtValue(v: unknown, unit?: Unit): string {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  switch (unit) {
    case "currency":
      return money(n);
    case "count":
      return indianInt(n);
    case "percent":
      return `${n.toFixed(2)}%`;
    case "time":
      return clock(n);
    default:
      return Math.abs(n) < 1000 ? n.toFixed(1) : indianInt(n);
  }
}

/** Short tick labels for axes. */
export function fmtTick(v: number, unit?: Unit): string {
  if (unit === "percent") return `${Math.round(v)}%`;
  if (unit === "time") return clock(v);
  const a = Math.abs(v);
  const pre = unit === "currency" ? "₹" : "";
  if (a >= 1e7) return `${pre}${+(v / 1e7).toFixed(1)}Cr`;
  if (a >= 1e5) return `${pre}${+(v / 1e5).toFixed(1)}L`;
  if (a >= 1e3) return `${pre}${+(v / 1e3).toFixed(1)}K`;
  return `${pre}${Math.round(v)}`;
}

export function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}
