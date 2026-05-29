// Generic natural-language summary card used at the top of each section page.
// Renders an optional fact strip plus a list of {heading, text} narrative blocks.
export default function SectionSummary({ title, sub, facts, narrative }) {
  if (!narrative || narrative.length === 0) return null;
  return (
    <div className="card coach">
      <div className="coach-head">
        <h2>{title}</h2>
        {sub && <span className="sub">{sub}</span>}
      </div>

      {facts && facts.length > 0 && (
        <div className="coach-facts">
          {facts.map((f) => (
            <div className="coach-fact" key={f.label}>
              <span className="cf-value">{f.value}</span>
              <span className="cf-label">{f.label}</span>
            </div>
          ))}
        </div>
      )}

      {narrative.map((block) => (
        <div className="coach-block" key={block.heading}>
          <h3>{block.heading}</h3>
          <p>{block.text}</p>
        </div>
      ))}
    </div>
  );
}
