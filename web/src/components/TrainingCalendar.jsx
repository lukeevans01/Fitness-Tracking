import { useMemo, useState } from "react";
import { DayCard } from "./PlanSection.jsx";
import PlanEditor from "./PlanEditor.jsx";
import useJson from "../useJson.js";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const KIND_SHORT = { run: "Run", strength: "Lift", rest: "Rest" };

// ---- date helpers (local, ISO YYYY-MM-DD; no timezone drift) ----
const parse = (iso) => new Date(iso + "T00:00:00");
const toIso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function addDays(iso, n) { const d = parse(iso); d.setDate(d.getDate() + n); return toIso(d); }
function addMonths(iso, n) { const d = parse(iso); d.setDate(1); d.setMonth(d.getMonth() + n); return toIso(d); }
function mondayOf(iso) { const d = parse(iso); const wd = (d.getDay() + 6) % 7; d.setDate(d.getDate() - wd); return toIso(d); }
function firstOfMonth(iso) { return iso.slice(0, 7) + "-01"; }
const daysBetween = (a, b) => Math.round((parse(a) - parse(b)) / 86400000);

function fmtLong(iso) {
  return parse(iso).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}
function fmtMonth(iso) {
  return parse(iso).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}
function fmtWeekRange(weekStart) {
  const ws = parse(weekStart), we = parse(addDays(weekStart, 6));
  const wsM = ws.toLocaleString("en-GB", { month: "short" });
  const weM = we.toLocaleString("en-GB", { month: "short" });
  return ws.getMonth() === we.getMonth()
    ? `${ws.getDate()}–${we.getDate()} ${weM}`
    : `${ws.getDate()} ${wsM} – ${we.getDate()} ${weM}`;
}

// Turn an edited session back into the compact day shape the calendar renders,
// so a pending change previews immediately before the rebuild lands.
function previewDay(day, session) {
  if (!day || !session) return day;
  return {
    ...day,
    session_type: session.session_type,
    session_kind: session.session_kind,
    duration_min: session.duration_min,
    run: session.run_details,
    exercises: session.exercises,
    details: session.details,
    purpose: session.purpose,
    source: "override",
  };
}

function oneLiner(day) {
  if (!day) return "";
  if (day.session_kind === "run" && day.run) {
    return [day.run.distance, day.run.pace].filter(Boolean).join(" · ");
  }
  if (day.session_kind === "strength" && day.exercises) {
    return `${day.exercises.length} exercise${day.exercises.length === 1 ? "" : "s"}`;
  }
  return day.duration_min ? `${day.duration_min} min` : "";
}

// ---- mini month navigator ----
function MiniMonth({ anchor, byDate, today, weekStart, selected, onPick, onPrev, onNext }) {
  const monthIdx = parse(anchor).getMonth();
  const gridStart = mondayOf(firstOfMonth(anchor));
  const weekEnd = addDays(weekStart, 6);

  const cells = [];
  for (let i = 0; i < 42; i++) {
    const iso = addDays(gridStart, i);
    const day = byDate[iso];
    const inMonth = parse(iso).getMonth() === monthIdx;
    const inWeek = iso >= weekStart && iso <= weekEnd;
    const kind = day && day.session_kind;
    const cls = [
      "mini-cell",
      inMonth ? "" : "mini-other",
      iso === today ? "mini-today" : "",
      iso === selected ? "mini-selected" : "",
      inWeek ? "mini-inweek" : "",
    ].filter(Boolean).join(" ");
    cells.push(
      <button type="button" key={iso} className={cls} onClick={() => onPick(iso)}>
        <span className="mini-num">{Number(iso.slice(8, 10))}</span>
        {kind && kind !== "rest" && <i className={`mini-dot mini-dot-${kind}`} />}
      </button>
    );
  }

  return (
    <div className="mini">
      <div className="mini-head">
        <button type="button" className="cal-arrow" onClick={onPrev} aria-label="Previous month">‹</button>
        <span className="mini-title">{fmtMonth(anchor)}</span>
        <button type="button" className="cal-arrow" onClick={onNext} aria-label="Next month">›</button>
      </div>
      <div className="mini-dow">{DOW.map((d) => <span key={d}>{d[0]}</span>)}</div>
      <div className="mini-grid">{cells}</div>
    </div>
  );
}

// ---- big week view ----
function WeekView({ weekStart, byDate, today, selected, edits, onSelect, onPrev, onNext }) {
  return (
    <div className="wk">
      <div className="wk-head">
        <button type="button" className="cal-arrow" onClick={onPrev} aria-label="Previous week">‹</button>
        <span className="wk-title">{fmtWeekRange(weekStart)}</span>
        <button type="button" className="cal-arrow" onClick={onNext} aria-label="Next week">›</button>
      </div>
      <div className="wk-grid">
        {Array.from({ length: 7 }, (_, i) => {
          const iso = addDays(weekStart, i);
          const edit = edits[iso];
          let day = byDate[iso];
          if (edit && !edit.clear) day = previewDay(day, edit.session);
          const kind = (day && day.session_kind) || "rest";
          const done = day && day.completed && (day.completed.run || day.completed.lift);
          const cls = [
            "wk-day",
            day ? `wk-day-${kind}` : "wk-day-empty",
            iso === today ? "wk-day-today" : "",
            iso === selected ? "wk-day-selected" : "",
          ].filter(Boolean).join(" ");
          return (
            <button type="button" key={iso} className={cls} onClick={() => day && onSelect(iso)} disabled={!day}>
              <span className="wk-dow">{DOW[i]} {Number(iso.slice(8, 10))}</span>
              {day ? (
                <>
                  <span className={`wk-kind plan-kind plan-kind-${kind}`}>{KIND_SHORT[kind] || kind}</span>
                  <span className="wk-type">{day.session_type}</span>
                  {oneLiner(day) && <span className="wk-sub">{oneLiner(day)}</span>}
                  <span className="wk-flags">
                    {done && <i className="cal-flag cal-flag-done">done</i>}
                    {edit && <i className="cal-flag cal-flag-edited">unsaved</i>}
                    {!edit && day.source === "override" && day.status !== "past" && (
                      <i className="cal-flag cal-flag-edited">edited</i>
                    )}
                  </span>
                </>
              ) : (
                <span className="wk-empty-note">—</span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function TrainingCalendar() {
  const { data, error } = useJson("/data/calendar.json");
  const [selSt, setSel] = useState(null);
  const [weekSt, setWeek] = useState(null);
  const [monthSt, setMonth] = useState(null);
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState({}); // { iso: { session } | { clear: true } }
  const [save, setSave] = useState({ state: "idle", message: "" });
  // Shared edit token, sent as X-Edit-Token so saving never relies on an
  // interactive auth redirect. Kept only in this browser, never in the bundle.
  const [token, setToken] = useState(() => {
    try { return localStorage.getItem("planEditToken") || ""; } catch { return ""; }
  });

  const byDate = useMemo(
    () => Object.fromEntries(((data && data.days) || []).map((d) => [d.date, d])),
    [data]
  );

  if (error) return null; // supplementary; fail quietly on the home hub
  if (!data || data.empty) return null;

  const today = data.today;
  const selected = selSt || today;
  const weekStart = weekSt || mondayOf(today);
  const monthAnchor = monthSt || firstOfMonth(today);
  const editCount = Object.keys(edits).length;

  const baseDay = byDate[selected];
  const pending = edits[selected];
  const detailDay = baseDay && pending && !pending.clear ? previewDay(baseDay, pending.session) : baseDay;
  const editable = baseDay && (baseDay.status === "today" || baseDay.status === "upcoming");

  // navigation
  function pickMiniDay(iso) { setSel(iso); setWeek(mondayOf(iso)); setEditing(false); }
  function selectWeekDay(iso) { setSel(iso); setEditing(false); }
  function stepWeek(delta) {
    const ws = addDays(weekStart, delta * 7);
    const off = Math.min(6, Math.max(0, daysBetween(selected, weekStart)));
    const newSel = addDays(ws, off);
    setWeek(ws); setSel(newSel); setMonth(firstOfMonth(newSel)); setEditing(false);
  }
  function stepMonth(delta) { setMonth(addMonths(monthAnchor, delta)); }

  // editing
  function applyEdit(session) { setEdits((p) => ({ ...p, [selected]: { session } })); setEditing(false); setSave({ state: "idle", message: "" }); }
  function clearDay() { setEdits((p) => ({ ...p, [selected]: { clear: true } })); setEditing(false); setSave({ state: "idle", message: "" }); }
  function discardDay() { setEdits((p) => { const n = { ...p }; delete n[selected]; return n; }); }
  function updateToken(v) {
    setToken(v);
    try { localStorage.setItem("planEditToken", v); } catch { /* ignore storage errors */ }
  }

  async function saveAll() {
    const payload = {
      profile_id: data.profile_id,
      edits: Object.entries(edits).map(([iso, v]) =>
        v.clear ? { iso_date: iso, clear: true } : { iso_date: iso, session: v.session }),
    };
    setSave({ state: "saving", message: "" });
    try {
      const headers = { "content-type": "application/json" };
      if (token) headers["x-edit-token"] = token;
      const res = await fetch("/api/plan-edit", {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });
      const raw = await res.text();
      let body;
      try { body = JSON.parse(raw); }
      catch { throw new Error("editing is temporarily unavailable, please try again in a moment"); }
      if (res.status === 401) throw new Error("edit token missing or incorrect, check the token below");
      if (!res.ok || !body.ok) throw new Error(body.error || `HTTP ${res.status}`);
      setSave({ state: "done", message: body.note || "Saved. Changes appear after the next rebuild." });
      setEdits({});
    } catch (e) {
      setSave({ state: "error", message: e.message });
    }
  }

  return (
    <div className="card cal-card">
      <div className="cal-head">
        <h2>Calendar</h2>
        <span className="sub">Pick a day in the month to jump there; step weeks with the arrows.</span>
      </div>

      <div className="cal-layout">
        <MiniMonth
          anchor={monthAnchor}
          byDate={byDate}
          today={today}
          weekStart={weekStart}
          selected={selected}
          onPick={pickMiniDay}
          onPrev={() => stepMonth(-1)}
          onNext={() => stepMonth(1)}
        />
        <WeekView
          weekStart={weekStart}
          byDate={byDate}
          today={today}
          selected={selected}
          edits={edits}
          onSelect={selectWeekDay}
          onPrev={() => stepWeek(-1)}
          onNext={() => stepWeek(1)}
        />
      </div>

      {detailDay && (
        <div className="cal-detail">
          <p className="cal-detail-meta">
            {fmtLong(detailDay.date)}
            {pending ? " · unsaved change" : detailDay.source === "override" ? " · edited" : ""}
            {detailDay.status === "completed" ? " · completed" : ""}
          </p>

          {editing ? (
            <PlanEditor
              day={baseDay}
              initial={pending && !pending.clear ? { ...baseDay, ...pending.session, run_details: pending.session.run_details } : null}
              onApply={applyEdit}
              onCancel={() => setEditing(false)}
            />
          ) : (
            <>
              {pending && pending.clear ? (
                <p className="sub">Will reset to the standard cycle session for this day.</p>
              ) : (
                <DayCard day={detailDay} />
              )}
              {editable && (
                <div className="cal-detail-actions">
                  <button type="button" className="pe-apply" onClick={() => setEditing(true)}>Edit session</button>
                  {detailDay.source === "override" && !pending && (
                    <button type="button" className="pe-cancel" onClick={clearDay}>Reset to template</button>
                  )}
                  {pending && (
                    <button type="button" className="pe-cancel" onClick={discardDay}>Discard this change</button>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {(editCount > 0 || save.state !== "idle") && (
        <div className={`cal-savebar cal-savebar-${save.state}`}>
          <span>
            {save.state === "done" ? save.message
              : save.state === "error" ? `Could not save: ${save.message}`
              : `${editCount} unsaved ${editCount === 1 ? "change" : "changes"}`}
          </span>
          {editCount > 0 && save.state !== "done" && (
            <div className="cal-savebar-actions">
              <input
                type="password"
                className="cal-token"
                placeholder="Edit token"
                value={token}
                onChange={(e) => updateToken(e.target.value)}
                autoComplete="off"
                aria-label="Edit token"
              />
              <button type="button" className="pe-cancel" onClick={() => { setEdits({}); setSave({ state: "idle", message: "" }); }}>Discard all</button>
              <button type="button" className="pe-apply" disabled={save.state === "saving"} onClick={saveAll}>
                {save.state === "saving" ? "Saving…" : "Save to plan"}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
