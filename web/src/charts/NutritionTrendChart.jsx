import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceLine, Legend,
} from "recharts";

function fmtDay(iso) {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export default function NutritionTrendChart({ days, targets }) {
  if (!days || days.length < 2) {
    return (
      <div className="card">
        <h2>Daily trend</h2>
        <p className="sub">
          Once you have logged a few days, calories and protein will trend here against your
          targets. {days && days.length === 1 ? "One day logged so far." : "Nothing logged yet."}
        </p>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Daily trend</h2>
      <p className="sub">Calories (bars) and protein (line) per day. Dashed lines are targets.</p>
      <ResponsiveContainer width="100%" height={280}>
        <ComposedChart data={days} margin={{ top: 12, right: 8, left: -8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="date" tickFormatter={fmtDay} fontSize={11} minTickGap={16} />
          <YAxis yAxisId="kcal" fontSize={11} width={48} />
          <YAxis yAxisId="prot" orientation="right" fontSize={11} width={40} unit="g" />
          <Tooltip labelFormatter={fmtDay} />
          <Legend />
          {targets.kcal && (
            <ReferenceLine yAxisId="kcal" y={targets.kcal} stroke="#9aa3af"
              strokeDasharray="4 4" />
          )}
          <Bar yAxisId="kcal" dataKey="kcal" name="kcal" fill="#1F3A5F" radius={[3, 3, 0, 0]} />
          <Line yAxisId="prot" type="monotone" dataKey="protein_g" name="protein (g)"
            stroke="#C0504D" strokeWidth={2} dot={{ r: 3 }} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
