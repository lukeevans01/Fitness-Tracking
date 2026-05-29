import useJson from "../useJson.js";
import Stat from "../components/Stat.jsx";
import SectionSummary from "../components/SectionSummary.jsx";
import E1rmChart from "../charts/E1rmChart.jsx";
import TonnageChart from "../charts/TonnageChart.jsx";
import LiftBalanceChart from "../charts/LiftBalanceChart.jsx";

const LIFTS = ["Squat", "Bench", "RDL", "OHP"];
const COLORS = { Squat: "#1F3A5F", Bench: "#C0504D", RDL: "#4F8A4F", OHP: "#E8A33D" };

function LiftProgress({ lift, current, target }) {
  const pct = current && target ? Math.min(100, Math.round((current / target) * 100)) : 0;
  return (
    <div className="lift-card">
      <div className="lift-card-head">
        <span className="lift-name">{lift}</span>
        <span className="lift-vals">
          {current ? `${Math.round(current)}` : "–"}
          <span className="lift-target-kg"> / {target} kg</span>
        </span>
      </div>
      <div className="lift-bar">
        <div className="lift-bar-fill" style={{ width: `${pct}%`, background: COLORS[lift] }} />
      </div>
    </div>
  );
}

export default function LiftingPage() {
  const { data, error } = useJson("/data/lifting.json");

  if (error) return <div className="error">Failed to load lifting data: {error}</div>;
  if (!data) return <div className="loading">Loading…</div>;
  if (data.empty) {
    return (
      <>
        <header><h1>Lifting</h1></header>
        <div className="card"><p className="sub">No strength data found yet.</p></div>
      </>
    );
  }

  const s = data.summary;
  const cur = data.current_e1rm || {};
  const bm = data.benchmarks || {};

  return (
    <>
      <header>
        <h1>Lifting</h1>
        <p>
          {s.first_session} – {s.last_session} · generated {data.generated_at}
        </p>
      </header>

      <div className="stats">
        <Stat value={s.total_sessions} label="Sessions" />
        <Stat value={s.sessions_4w} label="Last 4 weeks" />
        <Stat value={data.e1rm.length} label="Months tracked" />
      </div>

      <SectionSummary title="Last 4 weeks" narrative={data.recap} />

      <div className="card">
        <h2>Key lifts vs target</h2>
        <p className="sub">Current best e1RM (last 90 days) against your working target.</p>
        <div className="lift-progress-grid">
          {LIFTS.map((lift) => (
            <LiftProgress
              key={lift}
              lift={lift}
              current={cur[lift]}
              target={(bm[lift] || {}).target}
            />
          ))}
        </div>
      </div>

      <LiftBalanceChart current={cur} benchmarks={bm} />
      <E1rmChart e1rm={data.e1rm} benchmarks={bm} />
      <TonnageChart tonnage={data.tonnage} />
    </>
  );
}
