import useJson from "../useJson.js";
import Stat from "../components/Stat.jsx";
import CoachSummary from "../CoachSummary.jsx";
import ChatCoach from "../coach/ChatCoach.jsx";
import WeeklyVolumeChart from "../charts/WeeklyVolumeChart.jsx";
import PaceDisciplineChart from "../charts/PaceDisciplineChart.jsx";
import EfficiencyChart from "../charts/EfficiencyChart.jsx";
import YearlyVolumeChart from "../charts/YearlyVolumeChart.jsx";

export default function RunningPage() {
  const { data, error } = useJson("/data/running.json");

  if (error) return <div className="error">Failed to load running data: {error}</div>;
  if (!data) return <div className="loading">Loading…</div>;

  const s = data.summary;
  return (
    <>
      <header>
        <h1>Running</h1>
        <p>
          {s.first_run} – {s.last_run} · generated {data.generated_at}
        </p>
      </header>

      <div className="stats">
        <Stat value={s.total_runs} label="Runs" />
        <Stat value={`${Math.round(s.total_km).toLocaleString("en-GB")} km`} label="Total distance" />
        <Stat value={s.runs_with_hr} label="Runs with HR" />
        <Stat value={data.yearly.length} label="Years tracked" />
      </div>

      <CoachSummary recent={data.recent} />
      <ChatCoach data={data} />
      <WeeklyVolumeChart weekly={data.weekly} />
      <PaceDisciplineChart zoneTrend={data.zone_trend} easyTarget={data.easy_target_pct} />
      <EfficiencyChart efficiency={data.efficiency} />
      <YearlyVolumeChart yearly={data.yearly} />
    </>
  );
}
