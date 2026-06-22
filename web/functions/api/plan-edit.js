// Cloudflare Pages Function: POST /api/plan-edit
//
// Takes a batch of plan edits from the dashboard and fires a GitHub
// repository_dispatch. The apply-plan-edit workflow then writes the overrides
// into data/app.db (the same store the email coach uses), commits, and redeploys.
// Edits therefore go live a few minutes after saving, not instantly.
//
// Required Pages project env (set with `wrangler pages secret put <NAME>`):
//   GITHUB_DISPATCH_TOKEN  fine-grained PAT, contents:write + actions on the repo
//   GITHUB_REPO            "owner/repo", e.g. "lukeevans01/Fitness-Tracking"
// Optional:
//   PLAN_EDIT_TOKEN        shared edit token; if set, requests must send a matching
//                          X-Edit-Token header. Prefer gating the route with
//                          Cloudflare Access instead, in which case leave this unset.

const MAX_EDITS = 60;
const VALID_KINDS = new Set(["run", "strength", "rest"]);

export async function onRequestPost({ request, env }) {
  // Optional shared-token gate. Constant-time-ish compare; the primary gate
  // should be Cloudflare Access in front of this route.
  if (env.PLAN_EDIT_TOKEN) {
    const provided = request.headers.get("x-edit-token") || "";
    if (!timingSafeEqual(provided, env.PLAN_EDIT_TOKEN)) {
      return json({ error: "Unauthorised" }, 401);
    }
  }

  const token = env.GITHUB_DISPATCH_TOKEN;
  const repo = env.GITHUB_REPO;
  if (!token || !repo) {
    return json(
      { error: "Editing is not configured: GITHUB_DISPATCH_TOKEN / GITHUB_REPO not set." },
      503
    );
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }

  const validationError = validatePayload(payload);
  if (validationError) return json({ error: validationError }, 400);

  const clientPayload = {
    profile_id: payload.profile_id || "luke",
    edits: payload.edits,
  };

  try {
    const res = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "user-agent": "evansgale-plan-edit",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({ event_type: "plan-edit", client_payload: clientPayload }),
    });
    if (res.status !== 204) {
      const detail = await res.text();
      return json({ error: `GitHub dispatch failed (HTTP ${res.status})`, detail }, 502);
    }
  } catch (err) {
    return json({ error: `Dispatch call failed: ${err.message}` }, 502);
  }

  return json({
    ok: true,
    queued: payload.edits.length,
    note: "Saved. Your changes will appear on the live site in a few minutes once the rebuild finishes.",
  });
}

// ---------------------------------------------------------------- validation

function validatePayload(payload) {
  const edits = payload && Array.isArray(payload.edits) ? payload.edits : null;
  if (!edits || edits.length === 0) return "edits array is required";
  if (edits.length > MAX_EDITS) return `too many edits (max ${MAX_EDITS})`;

  // "today" in Amsterdam, used to reject past-dated edits client-side too.
  const todayIso = amsterdamTodayIso();

  for (const edit of edits) {
    if (!edit || typeof edit !== "object") return "each edit must be an object";
    const iso = edit.iso_date;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso || "")) return `invalid iso_date: ${iso}`;
    if (iso < todayIso) return `${iso}: cannot edit a past date`;
    if (edit.clear === true) continue;

    const s = edit.session;
    if (!s || typeof s !== "object") return `${iso}: session must be an object`;
    if (!VALID_KINDS.has(s.session_kind)) return `${iso}: invalid session_kind`;
    if (!s.session_type) return `${iso}: session_type is required`;
    if (s.exercises != null && !Array.isArray(s.exercises)) {
      return `${iso}: exercises must be a list`;
    }
  }
  return null;
}

function amsterdamTodayIso() {
  // en-CA gives YYYY-MM-DD; the timeZone shifts it to Amsterdam local date.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Amsterdam",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
