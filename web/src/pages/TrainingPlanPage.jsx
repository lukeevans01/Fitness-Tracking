import useJson from "../useJson.js";
import PlanSection from "../components/PlanSection.jsx";

export default function TrainingPlanPage() {
  const { data, error } = useJson("/data/plan.json");

  if (error) return <div className="error">Failed to load plan: {error}</div>;
  if (!data) return <div className="loading">Loading…</div>;
  if (data.empty) {
    return (
      <>
        <header><h1>Training plan</h1></header>
        <div className="card"><p className="sub">No plan template found yet.</p></div>
      </>
    );
  }

  return (
    <>
      <header>
        <h1>Training plan</h1>
        <p>Your repeating weekly cycle · generated {data.generated_at}</p>
      </header>
      <PlanSection plan={data} />
    </>
  );
}
