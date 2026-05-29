import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";

function fmtPace(decimalMin) {
  const m = Math.floor(decimalMin);
  const s = Math.round((decimalMin - m) * 60);
  return `${m}:${String(s).padStart(2, "0")}/km`;
}

function fmtMonth(key) {
  const [y, m] = key.split("-");
  const d = new Date(Number(y), Number(m) - 1, 1);
  return d.toLocaleDateString("en-GB", { month: "short", year: "2-digit" });
}

function EfTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null;
  const p = payload[0].payload;
  return (
    <div className="ef-tip">
      <strong>{fmtMonth(p.month)}</strong>
      <div>Efficiency: {p.ef.toFixed(3)} m/min per bpm</div>
      <div>Avg pace: {fmtPace(p.avg_pace)} at {p.avg_hr} bpm</div>
      <div>{p.runs} aerobic runs</div>
    </div>
  );
}

export default function EfficiencyChart({ efficiency }) {
  if (!efficiency || efficiency.length < 2) {
    return (
      <div className="card">
        <h2>Aerobic efficiency</h2>
        <p className="sub">Not enough heart-rate data yet to plot a trend.</p>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Aerobic efficiency</h2>
      <p className="sub">
        Speed per heartbeat (m/min per bpm) on aerobic runs, by month. Higher is fitter:
        more ground covered at the same effort. Rising over a block means the base is building.
      </p>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={efficiency} margin={{ top: 16, right: 12, left: -8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="month" tickFormatter={fmtMonth} fontSize={11} />
          <YAxis fontSize={11} width={48} domain={["auto", "auto"]} tickFormatter={(v) => v.toFixed(2)} />
          <Tooltip content={<EfTooltip />} />
          <Line
            type="monotone" dataKey="ef" stroke="var(--ink)" strokeWidth={2}
            dot={{ r: 3, fill: "var(--ink)" }} activeDot={{ r: 5 }}
          />
        </LineChart>
      </ResponsiveContainer>
      <p className="note">
        Aerobic runs only (easy and moderate pace), so interval sessions do not inflate the
        trend. Based on average HR per run, which Strava records for {efficiency.reduce((n, m) => n + m.runs, 0)} runs
        across {efficiency.length} months.
      </p>
    </div>
  );
}
