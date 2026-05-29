import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, LabelList,
} from "recharts";

export default function YearlyVolumeChart({ yearly }) {
  return (
    <div className="card">
      <h2>Yearly volume</h2>
      <p className="sub">Total km per calendar year across the full history.</p>
      <ResponsiveContainer width="100%" height={260}>
        <BarChart data={yearly} margin={{ top: 16, right: 8, left: -8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="year" fontSize={12} />
          <YAxis fontSize={11} unit=" km" width={56} />
          <Tooltip
            formatter={(value, name, props) => [
              `${value} km (${props.payload.runs} runs)`, "Volume",
            ]}
          />
          <Bar dataKey="km" fill="var(--ink)" radius={[4, 4, 0, 0]}>
            <LabelList dataKey="km" position="top" fontSize={11} formatter={(v) => Math.round(v)} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
