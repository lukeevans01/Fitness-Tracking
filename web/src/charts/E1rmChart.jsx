import { useState } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";

const LIFTS = ["Squat", "Bench", "RDL", "OHP"];
const COLORS = { Squat: "#1F3A5F", Bench: "#C0504D", RDL: "#4F8A4F", OHP: "#E8A33D" };
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

export default function E1rmChart({ e1rm, benchmarks }) {
  const [range, setRange] = useState("24m");
  if (!e1rm || e1rm.length === 0) {
    return (
      <div className="card">
        <h2>Estimated 1-rep max</h2>
        <p className="sub">No strength data yet.</p>
      </div>
    );
  }

  const months = RANGES.find((r) => r.key === range).months;
  const rows = months ? e1rm.slice(-months) : e1rm;

  return (
    <div className="card">
      <h2>Estimated 1-rep max</h2>
      <p className="sub">
        Best Epley e1RM per month on the key barbell lifts (kg). Dashed line is each lift's
        current target.
      </p>
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
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={rows} margin={{ top: 12, right: 12, left: -8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="month" tickFormatter={fmtMonth} fontSize={11} minTickGap={24} />
          <YAxis fontSize={11} unit=" kg" width={52} domain={["auto", "auto"]} />
          <Tooltip
            labelFormatter={fmtMonth}
            formatter={(v, name) => [v == null ? "no data" : `${v} kg`, name]}
          />
          <Legend />
          {LIFTS.map((lift) => (
            <Line
              key={lift}
              type="monotone"
              dataKey={lift}
              stroke={COLORS[lift]}
              strokeWidth={2}
              dot={{ r: 2 }}
              activeDot={{ r: 5 }}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <div className="lift-targets">
        {LIFTS.map((lift) => {
          const b = benchmarks && benchmarks[lift];
          if (!b) return null;
          return (
            <span className="lift-target" key={lift}>
              <span className="lt-dot" style={{ background: COLORS[lift] }} />
              {lift}: target {b.target} kg
            </span>
          );
        })}
      </div>
    </div>
  );
}
