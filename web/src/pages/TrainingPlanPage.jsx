import useJson from "../useJson.js";

const KIND_LABEL = { run: "Run", strength: "Strength", rest: "Rest" };

function DayCard({ day }) {
  const kind = day.session_kind;
  return (
    <div className={`plan-day plan-day-${kind}`}>
      <div className="plan-day-head">
        <span className="plan-day-label">{day.day_label}</span>
        <span className={`plan-kind plan-kind-${kind}`}>
          {KIND_LABEL[kind] || kind}
        </span>
      </div>
      <h3 className="plan-session">{day.session_type}</h3>
      {day.duration_min ? (
        <p className="plan-duration">{day.duration_min} min</p>
      ) : null}

      {day.run && (
        <dl className="plan-run">
          {day.run.distance && (
            <div><dt>Distance</dt><dd>{day.run.distance}</dd></div>
          )}
          {day.run.pace && <div><dt>Pace</dt><dd>{day.run.pace}</dd></div>}
          {day.run.hr_target && <div><dt>HR</dt><dd>{day.run.hr_target}</dd></div>}
        </dl>
      )}

      {day.exercises && day.exercises.length > 0 && (
        <ul className="plan-lifts">
          {day.exercises.map((e, i) => (
            <li key={i}>
              <span className="plan-lift-name">{e.name}</span>
              <span className="plan-lift-detail">
                {e.sets_reps}
                {e.weight ? ` · ${e.weight}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}

      {day.details && <p className="plan-details">{day.details}</p>}
      {day.purpose && <p className="plan-purpose">{day.purpose}</p>}
    </div>
  );
}

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

  const { race, block, days, hard_rules: hardRules } = data;

  return (
    <>
      <header>
        <h1>Training plan</h1>
        <p>Your repeating weekly cycle · generated {data.generated_at}</p>
      </header>

      <div className="card plan-race">
        <div className="plan-race-head">
          <div>
            <h2>{race.label}</h2>
            <p className="sub">Target {race.target}</p>
          </div>
          <div className="plan-countdown">
            <span className="plan-countdown-num">{race.weeks_to_race}</span>
            <span className="plan-countdown-label">weeks to race</span>
          </div>
        </div>
        <div className="plan-block">
          <span className={`plan-block-tag plan-kind-${block.name}`}>
            {block.name}
          </span>
          <p>{block.label}</p>
        </div>
      </div>

      <div className="plan-grid">
        {days.map((d, i) => <DayCard key={i} day={d} />)}
      </div>

      {hardRules && hardRules.length > 0 && (
        <div className="card">
          <h2>Hard rules</h2>
          <ul className="plan-rules">
            {hardRules.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}
    </>
  );
}
