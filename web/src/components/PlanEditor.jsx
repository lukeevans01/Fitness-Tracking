import { useState } from "react";

const KINDS = [
  { value: "run", label: "Run" },
  { value: "strength", label: "Strength" },
  { value: "rest", label: "Rest" },
];

// Build the editable session shape from a calendar day (or a pending edit already
// in that shape). The calendar carries a compact `run`; we map it to run_details
// so the saved override matches what the email coach reads.
function toSession(day) {
  const run = day.run_details || day.run || {};
  return {
    day_label: day.day_label || "",
    session_type: day.session_type || "",
    session_kind: day.session_kind || "rest",
    duration_min: day.duration_min ?? "",
    run_details: {
      pace: run.pace || "",
      hr_target: run.hr_target || "",
      distance: run.distance || "",
      effort: run.effort || "",
    },
    exercises: (day.exercises || []).map((e) => ({
      name: e.name || "",
      sets_reps: e.sets_reps || "",
      weight: e.weight || "",
    })),
    warm_up: day.warm_up || "",
    short_version: day.short_version || "",
    details: day.details || "",
    purpose: day.purpose || "",
    extras: day.extras || "",
  };
}

export default function PlanEditor({ day, initial, onApply, onCancel }) {
  const [s, setS] = useState(() => toSession(initial || day));

  const set = (k, v) => setS((prev) => ({ ...prev, [k]: v }));
  const setRun = (k, v) =>
    setS((prev) => ({ ...prev, run_details: { ...prev.run_details, [k]: v } }));
  const setEx = (i, k, v) =>
    setS((prev) => {
      const exercises = prev.exercises.slice();
      exercises[i] = { ...exercises[i], [k]: v };
      return { ...prev, exercises };
    });
  const addEx = () =>
    setS((prev) => ({ ...prev, exercises: [...prev.exercises, { name: "", sets_reps: "", weight: "" }] }));
  const removeEx = (i) =>
    setS((prev) => ({ ...prev, exercises: prev.exercises.filter((_, j) => j !== i) }));

  function apply(e) {
    e.preventDefault();
    const session = {
      ...s,
      duration_min: s.duration_min === "" ? null : Number(s.duration_min),
      exercises: s.exercises.filter((x) => x.name.trim()),
    };
    onApply(session);
  }

  const isRun = s.session_kind === "run";
  const isStrength = s.session_kind === "strength";

  return (
    <form className="pe-form" onSubmit={apply}>
      <div className="pe-row">
        <label className="pe-field pe-grow">
          <span>Session</span>
          <input value={s.session_type} onChange={(e) => set("session_type", e.target.value)}
            placeholder="e.g. Tempo run" required />
        </label>
        <label className="pe-field">
          <span>Type</span>
          <select value={s.session_kind} onChange={(e) => set("session_kind", e.target.value)}>
            {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
        </label>
        <label className="pe-field pe-narrow">
          <span>Minutes</span>
          <input type="number" min="0" max="600" value={s.duration_min}
            onChange={(e) => set("duration_min", e.target.value)} />
        </label>
      </div>

      {isRun && (
        <div className="pe-row">
          <label className="pe-field"><span>Pace</span>
            <input value={s.run_details.pace} onChange={(e) => setRun("pace", e.target.value)} placeholder="5:30/km" />
          </label>
          <label className="pe-field"><span>Distance</span>
            <input value={s.run_details.distance} onChange={(e) => setRun("distance", e.target.value)} placeholder="~10 km" />
          </label>
          <label className="pe-field"><span>HR</span>
            <input value={s.run_details.hr_target} onChange={(e) => setRun("hr_target", e.target.value)} placeholder="<150" />
          </label>
        </div>
      )}

      {isStrength && (
        <div className="pe-ex">
          <div className="pe-ex-head">
            <span>Exercises</span>
            <button type="button" className="pe-link" onClick={addEx}>+ add</button>
          </div>
          {s.exercises.map((ex, i) => (
            <div className="pe-ex-row" key={i}>
              <input value={ex.name} onChange={(e) => setEx(i, "name", e.target.value)} placeholder="Exercise" />
              <input value={ex.sets_reps} onChange={(e) => setEx(i, "sets_reps", e.target.value)} placeholder="3x8" />
              <input value={ex.weight} onChange={(e) => setEx(i, "weight", e.target.value)} placeholder="60kg" />
              <button type="button" className="pe-x" onClick={() => removeEx(i)} aria-label="Remove">×</button>
            </div>
          ))}
        </div>
      )}

      <label className="pe-field pe-grow">
        <span>Notes / details</span>
        <textarea rows="2" value={s.details} onChange={(e) => set("details", e.target.value)} />
      </label>

      <div className="pe-actions">
        <button type="submit" className="pe-apply">Apply change</button>
        <button type="button" className="pe-cancel" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
