import { useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";

const RANGES = [
  { key: "12m", label: "12m", months: 12 },
  { key: "24m", label: "24m", months: 24 },
  { key: "all", label: "All", months: null },
];

function fmtMonth(key) {
  const [y, m] = key.split("-");
  return new Date(Number(y), Number(m) - 1, 1).toLocaleDateString("en-GB", {
    month: "short", year: "2-digit",
  });
}

export default function TonnageChart({ tonnage }) {
  const [range, setRange] = useState("24m");
  if (!tonnage || tonnage.length === 0) {
    return (
      <div className="card">
        <h2>Training volume</h2>
        <p className="sub">No strength data yet.</p>
      </div>
    );
  }

  const months = RANGES.find((r) => r.key === range).months;
  const rows = months ? tonnage.slice(-months) : tonnage;

  return (
    <div className="card">
      <h2>Training volume</h2>
      <p className="sub">Total tonnage lifted per month (weight × reps, all exercises), in tonnes.</p>
      <div className="controls">
        {RANGES.map((r) => (
          <button
            key={r.key}
            className={range === r.key ? "active" : ""}
            onClick={() => setRange(r.key)}
          >
            {r.label}
          </button>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={rows} margin={{ top: 12, right: 12, left: -8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="month" tickFormatter={fmtMonth} fontSize={11} minTickGap={24} />
          <YAxis fontSize={11} unit=" t" width={44} />
          <Tooltip labelFormatter={fmtMonth} formatter={(v) => [`${v} tonnes`, "Volume"]} />
          <Bar dataKey="tonnes" fill="var(--ink)" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
