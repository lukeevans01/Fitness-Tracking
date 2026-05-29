// Shared running-coach logic for both the Cloudflare Pages Function (coach.js) and the
// Vite dev shim (vite.config.js). No secrets live here; the API key is passed in by the
// caller from its own environment. The persona mirrors specialists/running.py so the web
// coach and the email coach give consistent advice.
//
// Files prefixed with "_" are not routed by Cloudflare Pages Functions, so this is a
// plain importable module, not an endpoint.

const MODEL = "gemini-2.5-flash";
const ENDPOINT = (model, key) =>
  `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`;

// Mirrors specialists/running.py system_context(), trimmed for chat and given the
// advice-only boundary from coach_orchestrator.answer_training_question.
const PERSONA = `You are the running coach for Luke Evans. He is training for the San Sebastian marathon on 22 November 2026 with a sub-3:25 target (marathon pace 4:51/km).

TRAINING DISTRIBUTION
80/20 polarised: 80% of running volume at truly easy effort, 20% at quality effort. Luke's documented failure mode: his self-described easy runs drift to 5:30-5:45/km at HR 155-170, the moderate grey zone that is neither easy nor hard. In his 2025 marathon block 86% of km were moderate and 0% truly easy. The sub-3:25 target depends on fixing this. If Luke asks for easy, give him slow. Do not negotiate on HR caps.

PACE ZONES
- Easy / recovery:   5:35-6:00/km, HR <150 (conversational)
- General aerobic:   5:25-5:45/km, HR 150-160 (long run base)
- Marathon pace:     4:51/km,      HR 165-170
- Lactate threshold: 4:30-4:40/km, HR 172-178 (comfortably hard)
- 5k / VO2max:       4:00-4:15/km, HR 180+ (intervals)

SESSION HIERARCHY (value for sub-3:25, highest first)
1. Marathon-pace runs. 2. Long runs (time on feet). 3. Threshold (max one a week, easy day after). 4. Easy recovery runs (keep the HR cap). 5. Strides.

CONSTRAINTS
- Squash on Tuesday evenings counts as the weekly intensity session if played. Do not add a quality run that day or the day before.
- Sleep under 6h: drop any run to a 20-30 min walk or skip. First baby born late May 2026, so sleep deprivation is an ongoing factor.
- No PB attempts in training.

STYLE
British spelling. No em-dashes. No emojis. No motivational fluff. Be direct, specific and concise (2-5 sentences typical). Give concrete numbers (paces, HR, distances) rather than vague guidance.

BOUNDARY
This is ADVICE ONLY. Do not output a full prescribed daily plan or claim to have changed his schedule. If he wants his plan changed, tell him to reply to one of his daily coaching emails, which is where session overrides are applied.`;

function contextBlock(context) {
  if (!context || !context.recent) return "";
  const r = context.recent;
  const lines = [
    "LUKE'S RECENT RUNNING (use these real numbers, do not invent others):",
    `- Window: last ${r.weeks} weeks (${r.window_start} to ${r.window_end})`,
    `- ${r.runs} runs, ${r.km} km total, ${r.avg_km_per_week} km per week, longest ${r.longest_km} km`,
    `- Pace split of classified km: ${r.easy_pct}% easy, ${r.moderate_pct}% moderate, ${r.quality_pct}% quality`,
    r.trend_pct === null || r.trend_pct === undefined
      ? null
      : `- Volume trend vs previous block: ${r.trend_pct >= 0 ? "+" : ""}${r.trend_pct}%`,
    r.weeks_to_race ? `- About ${r.weeks_to_race} weeks to race day` : null,
  ].filter(Boolean);
  if (Array.isArray(context.recent_weeks) && context.recent_weeks.length) {
    lines.push("- Recent weekly km (oldest to newest): " +
      context.recent_weeks.map((w) => `${w.week_start}:${w.km}`).join(", "));
  }
  return lines.join("\n");
}

// Build the Gemini request body from chat history + context.
export function buildRequest(messages, context) {
  const ctx = contextBlock(context);
  const systemText = ctx ? `${PERSONA}\n\n${ctx}` : PERSONA;
  const contents = (messages || [])
    .filter((m) => m && m.text && (m.role === "user" || m.role === "coach"))
    .map((m) => ({
      role: m.role === "coach" ? "model" : "user",
      parts: [{ text: m.text }],
    }));
  return {
    systemInstruction: { parts: [{ text: systemText }] },
    contents,
    generationConfig: { temperature: 0.4, maxOutputTokens: 600 },
  };
}

// Call Gemini and return the plain-text answer. Throws on HTTP / parse failure.
export async function callGemini(apiKey, messages, context, model = MODEL) {
  const body = buildRequest(messages, context);
  const res = await fetch(ENDPOINT(model, apiKey), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`Gemini HTTP ${res.status}: ${detail.slice(0, 300)}`);
  }
  const data = await res.json();
  const text = data?.candidates?.[0]?.content?.parts
    ?.map((p) => p.text || "")
    .join("")
    .trim();
  if (!text) throw new Error("Gemini returned no text");
  return text;
}

// Deterministic local fallback so the chat UI is demonstrable without an API key (used by
// the dev shim only). Grounded in the same context numbers the real coach would see.
export function stubAnswer(messages, context) {
  const last = [...(messages || [])].reverse().find((m) => m.role === "user");
  const q = last ? last.text : "";
  const r = context && context.recent;
  const easy = r ? `${r.easy_pct}% easy / ${r.moderate_pct}% moderate / ${r.quality_pct}% quality` : "your recent splits";
  return (
    `[local preview - no GEMINI_API_KEY set, so this is a deterministic stub rather than the live coach]\n\n` +
    `You asked: "${q}". Over your last ${r ? r.weeks : 4} weeks your classified running was ${easy}. ` +
    `The clear priority is shifting volume out of the moderate grey zone: hold easy runs at 5:35-6:00/km with HR under 150, ` +
    `and reserve quality for one session a week. Set GEMINI_API_KEY to get live, conversational coaching here.`
  );
}

export { PERSONA, MODEL };
