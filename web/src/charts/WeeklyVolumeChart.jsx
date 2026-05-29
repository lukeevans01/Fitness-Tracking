import { useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";

const ZONES = [
  { key: "easy_km", name: "Easy (>=6:00/km)", color: "var(--easy)" },
  { key: "moderate_km", name: "Moderate (5:06-5:59)", color: "var(--moderate)" },
  { key: "quality_km", name: "Quality (<5:06)", color: "var(--quality)" },
  { key: "unzoned_km", name: "No pace data", color: "var(--unzoned)" },
];

const RANGES = [
  { label: "12w", weeks: 12 },
  { label: "26w", weeks: 26 },
  { label: "1y", weeks: 52 },
  { label: "All", weeks: Infinity },
];

function fmtWeek(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export default function WeeklyVolumeChart({ weekly }) {
  const [range, setRange] = useState(26);
  const data = range === Infinity ? weekly : weekly.slice(-range);

  return (
    <div className="card">
      <h2>Weekly running volume by pace zone</h2>
      <p className="sub">
        Total km per week, split by the pace zone of each run. The 80/20 target is mostly green.
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
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="week_start" tickFormatter={fmtWeek} fontSize={11}
                 interval="preserveStartEnd" minTickGap={24} />
          <YAxis fontSize={11} unit=" km" width={56} />
          <Tooltip
            labelFormatter={(v) => `Week of ${fmtWeek(v)}`}
            formatter={(value, name) => [`${value} km`, name]}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {ZONES.map((z) => (
            <Bar key={z.key} dataKey={z.key} stackId="km" name={z.name} fill={z.color} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <p className="note">
        Per-run classification by average pace — the Strava export has no intra-run zone time,
        so interval sessions show at their average. Good for trend, not precise time-in-zone.
      </p>
    </div>
  );
}
