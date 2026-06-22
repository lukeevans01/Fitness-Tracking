import { Link } from "react-router-dom";
import useJson from "../useJson.js";
import { useProfile } from "../ProfileContext.jsx";
import PlanSection from "../components/PlanSection.jsx";
import TrainingCalendar from "../components/TrainingCalendar.jsx";

const SECTIONS = [
  { key: "running", title: "Running", to: "/running",
    blurb: "Volume, pace zones and aerobic efficiency toward the marathon." },
  { key: "lifting", title: "Lifting", to: "/lifting",
    blurb: "Strength trend on the key lifts and training volume." },
  { key: "nutrition", title: "Nutrition", to: "/nutrition",
    blurb: "Daily macros and calories against your targets." },
];

function SectionCard({ title, to, blurb, headline }) {
  const kpis = (headline && headline.kpis) || [];
  return (
    <Link to={to} className="home-card">
      <div className="home-card-head">
        <h2>{title}</h2>
        <span className="home-card-cta">View details →</span>
      </div>
      <p className="home-card-blurb">{blurb}</p>
      {kpis.length > 0 && (
        <div className="home-kpis">
          {kpis.map((k, i) => (
            <div className="home-kpi" key={i}>
              <span className="hk-value">{k.value}</span>
              <span className="hk-label">{k.label}</span>
            </div>
          ))}
        </div>
      )}
      {headline && headline.narrative && (
        <p className="home-card-narrative">{headline.narrative}</p>
      )}
    </Link>
  );
}

export default function Home() {
  const { active } = useProfile();
  const { data, error } = useJson("/data/home.json");
  const { data: plan } = useJson("/data/plan.json");

  if (error) return <div className="error">Failed to load summary: {error}</div>;
  if (!data) return <div className="loading">Loading…</div>;

  const sections = data.sections || {};
  const planHeadline = data.plan;
  return (
    <>
      <header>
        <h1>{active.name}</h1>
        <p>Training overview · generated {data.generated_at}</p>
      </header>

      {planHeadline && planHeadline.block_name && (
        <Link to="/plan" className="home-plan">
          <div>
            <span className="home-plan-block">{planHeadline.block_name} phase</span>
            <span className="home-plan-text">
              {planHeadline.weeks_to_race} weeks to the {planHeadline.race_label}
            </span>
          </div>
          <span className="home-card-cta">View plan →</span>
        </Link>
      )}

      <div className="home-grid">
        {SECTIONS.map(({ key, ...rest }) => (
          <SectionCard key={key} {...rest} headline={sections[key]} />
        ))}
      </div>

      {plan && !plan.empty && (
        <section className="home-section">
          <div className="home-section-head">
            <h2>Training plan</h2>
            <Link to="/plan" className="home-card-cta">Open plan tab →</Link>
          </div>
          <TrainingCalendar />
          <PlanSection plan={plan} embedded />
        </section>
      )}
    </>
  );
}
