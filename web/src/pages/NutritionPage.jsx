import useJson from "../useJson.js";
import Stat from "../components/Stat.jsx";
import SectionSummary from "../components/SectionSummary.jsx";
import NutritionTrendChart from "../charts/NutritionTrendChart.jsx";

const MACROS = [
  { key: "kcal", label: "Calories", unit: "", color: "#1F3A5F" },
  { key: "protein_g", label: "Protein", unit: "g", color: "#C0504D" },
  { key: "carbs_g", label: "Carbs", unit: "g", color: "#E8A33D" },
  { key: "fat_g", label: "Fat", unit: "g", color: "#4F8A4F" },
];

function MacroBar({ label, unit, color, value, target }) {
  const pct = value && target ? Math.min(100, Math.round((value / target) * 100)) : 0;
  return (
    <div className="macro-row">
      <div className="macro-row-head">
        <span className="macro-label">{label}</span>
        <span className="macro-vals">
          {value == null ? "–" : Math.round(value)}
          {unit}
          <span className="macro-target"> / {target ? Math.round(target) : "?"}{unit}</span>
        </span>
      </div>
      <div className="macro-bar">
        <div className="macro-bar-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

export default function NutritionPage() {
  const { data, error } = useJson("/data/nutrition.json");

  if (error) return <div className="error">Failed to load nutrition data: {error}</div>;
  if (!data) return <div className="loading">Loading…</div>;

  const targets = data.targets || {};
  const days = data.days || [];
  const latest = days.length ? days[days.length - 1] : null;
  const avg = data.averages || {};

  return (
    <>
      <header>
        <h1>Nutrition</h1>
        <p>
          {data.summary.days_logged} day(s) logged
          {data.summary.last_day ? ` · latest ${data.summary.last_day}` : ""}
          {" · "}generated {data.generated_at}
        </p>
      </header>

      {data.empty ? (
        <div className="card">
          <h2>No nutrition logged yet</h2>
          <p className="sub">
            Reply to one of your daily coaching emails with what you ate and it will be
            parsed into macros and tracked here against your targets ({targets.kcal} kcal,
            {" "}{targets.protein_g}g protein, {targets.carbs_g}g carbs, {targets.fat_g}g fat).
          </p>
        </div>
      ) : (
        <>
          <div className="stats">
            <Stat value={data.summary.days_logged} label="Days logged" />
            <Stat value={avg.kcal != null ? Math.round(avg.kcal) : "–"} label="Avg kcal" />
            <Stat value={avg.protein_g != null ? `${Math.round(avg.protein_g)}g` : "–"} label="Avg protein" />
          </div>

          <SectionSummary title="Recent nutrition" narrative={data.recap} />

          <div className="card">
            <h2>Latest day vs target</h2>
            <p className="sub">{latest ? latest.date : ""} against your daily targets.</p>
            <div className="macro-list">
              {MACROS.map((m) => (
                <MacroBar
                  key={m.key}
                  label={m.label}
                  unit={m.unit}
                  color={m.color}
                  value={latest ? latest[m.key] : null}
                  target={targets[m.key]}
                />
              ))}
            </div>
          </div>

          <NutritionTrendChart days={days} targets={targets} />
        </>
      )}
    </>
  );
}
