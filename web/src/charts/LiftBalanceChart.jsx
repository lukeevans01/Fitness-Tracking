import {
  RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar,
  ResponsiveContainer, Tooltip,
} from "recharts";

const LIFTS = ["Squat", "Bench", "RDL", "OHP"];

export default function LiftBalanceChart({ current, benchmarks }) {
  const cur = current || {};
  const bm = benchmarks || {};

  const data = LIFTS.filter((l) => cur[l] != null && (bm[l] || {}).target).map((l) => {
    const target = bm[l].target;
    return {
      lift: l,
      pct: Math.round((cur[l] / target) * 100),
      current: Math.round(cur[l]),
      target,
    };
  });

  if (data.length < 3) {
    return (
      <div className="card">
        <h2>Strength balance</h2>
        <p className="sub">Needs recent estimates on at least three key lifts to plot.</p>
      </div>
    );
  }

  const maxPct = Math.max(110, ...data.map((d) => d.pct));

  return (
    <div className="card">
      <h2>Strength balance</h2>
      <p className="sub">
        Each lift's current e1RM as a share of its target. A balanced ring near 100%
        means every lift is tracking together; dents show what is lagging.
      </p>
      <ResponsiveContainer width="100%" height={300}>
        <RadarChart data={data} margin={{ top: 8, right: 24, bottom: 8, left: 24 }}>
          <PolarGrid />
          <PolarAngleAxis dataKey="lift" fontSize={13}
            tick={{ fill: "var(--ink)" }} />
          <PolarRadiusAxis domain={[0, maxPct]} angle={90} fontSize={10}
            tickFormatter={(v) => `${v}%`} />
          <Radar name="% of target" dataKey="pct" stroke="#1F3A5F"
            fill="#1F3A5F" fillOpacity={0.35} />
          <Tooltip
            formatter={(value, _n, p) => [
              `${value}% (${p.payload.current} / ${p.payload.target} kg)`,
              p.payload.lift,
            ]}
          />
        </RadarChart>
      </ResponsiveContainer>
      <p className="note">
        100% is your working target for each lift. Pull-ups are excluded (bodyweight is
        not in the export, so an estimate would mislead).
      </p>
    </div>
  );
}
