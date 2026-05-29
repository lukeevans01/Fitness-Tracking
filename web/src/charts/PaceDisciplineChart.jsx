import { useState } from "react";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, ReferenceLine,
} from "recharts";

const ZONES = [
  { key: "easy_pct", name: "Easy", color: "var(--easy)" },
  { key: "moderate_pct", name: "Moderate", color: "var(--moderate)" },
  { key: "quality_pct", name: "Quality", color: "var(--quality)" },
];

const RANGES = [
  { label: "26w", weeks: 26 },
  { label: "1y", weeks: 52 },
  { label: "2y", weeks: 104 },
  { label: "All", weeks: Infinity },
];

function fmtWeek(iso) {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export default function PaceDisciplineChart({ zoneTrend, easyTarget = 70 }) {
  const [range, setRange] = useState(52);
  if (!zoneTrend || zoneTrend.length < 2) {
    return (
      <div className="card">
        <h2>Easy-running discipline</h2>
        <p className="sub">Once a few weeks of paced runs are logged, your easy share trends here.</p>
      </div>
    );
  }
  const data = range === Infinity ? zoneTrend : zoneTrend.slice(-range);

  return (
    <div className="card">
      <h2>Easy-running discipline</h2>
      <p className="sub">
        Rolling 4-week share of running by pace zone. The aim is the green easy band
        filling up to the {easyTarget}% line.
      </p>
      <div className="controls">
        {RANGES.map((r) => (
          <button
            key={r.label}
            className={range === r.weeks ? "active" : ""}
            onClick={() => setRange(r.weeks)}
          >
            {r.label}
          </button>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 8 }}
          stackOffset="expand">
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="week_start" tickFormatter={fmtWeek} fontSize={11}
                 interval="preserveStartEnd" minTickGap={24} />
          <YAxis fontSize={11} width={40} tickFormatter={(v) => `${Math.round(v * 100)}%`} />
          <Tooltip
            labelFormatter={(v) => `Week of ${fmtWeek(v)}`}
            formatter={(value, name) => [`${value}%`, name]}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <ReferenceLine y={easyTarget / 100} stroke="#1F3A5F" strokeDasharray="5 4"
            strokeWidth={1.5} ifOverflow="extendDomain" />
          {ZONES.map((z) => (
            <Area key={z.key} dataKey={z.key} stackId="pct" name={z.name}
              stroke={z.color} fill={z.color} fillOpacity={0.85} />
          ))}
        </AreaChart>
      </ResponsiveContainer>
      <p className="note">
        Per-run classification by average pace, smoothed over the trailing 4 weeks.
        The dashed line is your {easyTarget}% easy target.
      </p>
    </div>
  );
}
