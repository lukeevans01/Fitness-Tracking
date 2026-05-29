// Cloudflare Pages Function: POST /api/coach
// The Gemini API key lives in the Pages project env (a server-side secret) and is never
// exposed to the browser. Set it with:  wrangler pages secret put GEMINI_API_KEY
import { callGemini } from "./_coach.js";

export async function onRequestPost({ request, env }) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }

  const messages = Array.isArray(payload?.messages) ? payload.messages : null;
  if (!messages || messages.length === 0) {
    return json({ error: "messages array is required" }, 400);
  }

  const apiKey = env.GEMINI_API_KEY;
  if (!apiKey) {
    return json(
      { error: "Coach is not configured: GEMINI_API_KEY is not set." },
      503
    );
  }

  try {
    const answer = await callGemini(apiKey, messages, payload.context);
    return json({ answer });
  } catch (err) {
    return json({ error: `Coach call failed: ${err.message}` }, 502);
  }
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
