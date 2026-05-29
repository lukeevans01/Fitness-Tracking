function fmtRange(startIso, endIso) {
  const opts = { day: "numeric", month: "short" };
  const start = new Date(startIso).toLocaleDateString("en-GB", opts);
  const end = new Date(endIso).toLocaleDateString("en-GB", opts);
  return `${start} – ${end}`;
}

function TrendBadge({ pct }) {
  if (pct === null || pct === undefined) return null;
  const dir = pct >= 8 ? "up" : pct <= -8 ? "down" : "flat";
  const label = dir === "up" ? `▲ ${pct}%` : dir === "down" ? `▼ ${Math.abs(pct)}%` : "level";
  return <span className={`trend trend-${dir}`}>{label}</span>;
}

export default function CoachSummary({ recent }) {
  if (!recent) return null;

  const facts = [
    { value: recent.runs, label: "runs" },
    { value: `${Math.round(recent.avg_km_per_week)} km`, label: "avg / week" },
    { value: `${Math.round(recent.longest_km)} km`, label: "longest" },
    { value: recent.easy_pct === null ? "–" : `${recent.easy_pct}%`, label: "easy share" },
  ];

  return (
    <div className="card coach">
      <div className="coach-head">
        <h2>Last {recent.weeks} weeks</h2>
        <span className="sub">
          {fmtRange(recent.window_start, recent.window_end)}
          {recent.weeks_to_race > 0 ? ` · ${recent.weeks_to_race} weeks to race day` : ""}
        </span>
        <TrendBadge pct={recent.trend_pct} />
      </div>

      <div className="coach-facts">
        {facts.map((f) => (
          <div className="coach-fact" key={f.label}>
            <span className="cf-value">{f.value}</span>
            <span className="cf-label">{f.label}</span>
          </div>
        ))}
      </div>

      {recent.narrative.map((block) => (
        <div className="coach-block" key={block.heading}>
          <h3>{block.heading}</h3>
          <p>{block.text}</p>
        </div>
      ))}

      <p className="coach-foot">
        Generated from your training data and refreshed every week. Ask the coach
        below for anything more specific.
      </p>
    </div>
  );
}
