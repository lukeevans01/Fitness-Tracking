// Cloudflare Pages Function: POST /api/telegram  (Telegram Bot API webhook)
//
// Verifies the request really came from Telegram, checks it came from the one chat allowed
// to drive the bot, then hands the update to GitHub Actions via repository_dispatch. The
// heavy lifting (coach calls, store writes) happens there, where the Python lives.
//
// Telegram retries any non-200 response, so this returns 200 for anything it has finished
// with, including updates it deliberately drops. It only returns non-200 when a retry could
// genuinely help.
//
// This path is exempt from the site's Basic auth in web/functions/_middleware.js, because
// Telegram cannot send an Authorization header. The secret token below is what protects it.
//
// Required Pages project env:
//   TELEGRAM_WEBHOOK_SECRET   the secret_token given to setWebhook
//   TELEGRAM_CHAT_ID          the only chat allowed to drive the bot
//   GITHUB_DISPATCH_TOKEN     fine-grained PAT, contents:write
//   GITHUB_REPO               "owner/repo"
// Optional:
//   TELEGRAM_BOT_TOKEN        enables the instant "got it" acknowledgement

const DISPATCH_EVENT = "telegram-message";

export async function onRequestPost({ request, env }) {
  // 1. Prove it is Telegram. Without this the endpoint is open to anyone who finds it.
  const secret = env.TELEGRAM_WEBHOOK_SECRET;
  if (!secret) {
    console.log("TELEGRAM_WEBHOOK_SECRET is not set; refusing to process.");
    return json({ ok: true, skipped: "not configured" });
  }
  const presented = request.headers.get("x-telegram-bot-api-secret-token") || "";
  if (!timingSafeEqual(presented, secret)) {
    // 403 rather than 401: there is nothing to retry and no credentials to offer.
    return json({ ok: false, error: "forbidden" }, 403);
  }

  let update;
  try {
    update = await request.json();
  } catch {
    // Malformed body will never parse on a retry, so accept and drop it.
    return json({ ok: true, skipped: "unparseable body" });
  }

  // 2. Only the configured chat may drive the bot. Anyone can message a bot they find.
  const chatId = extractChatId(update);
  const expected = env.TELEGRAM_CHAT_ID;
  if (!expected || String(chatId) !== String(expected)) {
    console.log(`Dropping update from unexpected chat ${chatId}.`);
    // Silently accepted: replying would confirm the bot is live to whoever probed it.
    return json({ ok: true, skipped: "unauthorised chat" });
  }

  // 3. Acknowledge immediately. The real reply comes minutes later from Actions, and a
  //    silent chat in the meantime reads as a broken bot.
  await acknowledge(env, update).catch((err) =>
    console.log(`Acknowledgement failed (not fatal): ${err.message}`)
  );

  // 4. Hand off to Actions.
  const token = env.GITHUB_DISPATCH_TOKEN;
  const repo = env.GITHUB_REPO;
  if (!token || !repo) {
    return json({ ok: false, error: "GITHUB_DISPATCH_TOKEN / GITHUB_REPO not set" }, 503);
  }
  try {
    const res = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "user-agent": "evansgale-telegram",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({
        event_type: DISPATCH_EVENT,
        // client_payload is capped at 64KB; an update is far smaller, but send only what
        // the handler needs rather than the whole envelope.
        client_payload: { update: trimUpdate(update) },
      }),
    });
    if (res.status !== 204) {
      const detail = await res.text();
      // 502 so Telegram retries: a transient GitHub failure is worth another attempt.
      return json({ ok: false, error: `dispatch failed (HTTP ${res.status})`, detail }, 502);
    }
  } catch (err) {
    return json({ ok: false, error: `dispatch call failed: ${err.message}` }, 502);
  }

  return json({ ok: true, queued: true });
}

function extractChatId(update) {
  const message = update?.message || update?.edited_message;
  if (message?.chat?.id !== undefined) return message.chat.id;
  return update?.callback_query?.message?.chat?.id;
}

/** Keep the fields the Python router reads, and drop the rest. */
function trimUpdate(update) {
  const out = { update_id: update.update_id };
  for (const key of ["message", "edited_message"]) {
    const m = update[key];
    if (m) {
      out[key] = {
        message_id: m.message_id,
        text: m.text,
        chat: { id: m.chat?.id },
      };
    }
  }
  const cb = update.callback_query;
  if (cb) {
    out.callback_query = {
      id: cb.id,
      data: cb.data,
      message: {
        message_id: cb.message?.message_id,
        chat: { id: cb.message?.chat?.id },
      },
    };
  }
  return out;
}

/**
 * Stop a tapped button spinning, or mark a typed message as received. Best effort: a failed
 * acknowledgement must not stop the update being handled.
 */
async function acknowledge(env, update) {
  const botToken = env.TELEGRAM_BOT_TOKEN;
  if (!botToken) return;

  const callbackId = update?.callback_query?.id;
  if (callbackId) {
    await fetch(`https://api.telegram.org/bot${botToken}/answerCallbackQuery`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ callback_query_id: callbackId, text: "Got it, working on it..." }),
    });
    return;
  }

  const chatId = extractChatId(update);
  if (!chatId) return;
  await fetch(`https://api.telegram.org/bot${botToken}/sendChatAction`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, action: "typing" }),
  });
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
