// Cloudflare Pages middleware: HTTP Basic auth across the whole site.
//
// The dashboard carries weight, nutrition and training history, so it should not be
// public. Basic auth is used rather than Cloudflare Access for one specific reason: the
// browser re-sends Basic credentials automatically on every request, including the
// background fetch the plan editor makes to /api/plan-edit. Access answers an
// unauthenticated fetch with a cross-origin redirect to its login page, which a fetch
// cannot follow, and that surfaces as "Failed to fetch".
//
// Required Pages project env (set as an encrypted secret):
//   SITE_AUTH_PASSWORD   the password. If unset, the site stays open — this mirrors the
//                        optional PLAN_EDIT_TOKEN gate and means deploying this file
//                        before setting the secret cannot lock you out.
// Optional:
//   SITE_AUTH_USER       username, default "luke".
//
// Paths that authenticate themselves and must not be challenged. Inbound webhooks cannot
// send an Authorization header, so they carry their own shared secret instead.
const AUTH_EXEMPT = ["/api/telegram"];

const REALM = "Evansgale";

export async function onRequest(context) {
  const { request, env, next } = context;
  const password = env.SITE_AUTH_PASSWORD;

  // No password configured: leave the site open rather than hard-locking it.
  if (!password) return next();

  const { pathname } = new URL(request.url);
  if (AUTH_EXEMPT.some((p) => pathname === p || pathname.startsWith(p + "/"))) {
    return next();
  }

  const expectedUser = env.SITE_AUTH_USER || "luke";
  const header = request.headers.get("authorization") || "";

  if (authorised(header, expectedUser, password)) return next();

  return new Response("Unauthorised", {
    status: 401,
    headers: {
      // Prompts the browser for credentials; password managers can then store them.
      "WWW-Authenticate": `Basic realm="${REALM}", charset="UTF-8"`,
      "cache-control": "no-store",
    },
  });
}

function authorised(header, expectedUser, expectedPassword) {
  const [scheme, encoded] = header.split(" ");
  if (!encoded || scheme.toLowerCase() !== "basic") return false;

  let decoded;
  try {
    decoded = atob(encoded);
  } catch {
    return false;
  }

  // Only the first colon separates user from password; passwords may contain colons.
  const sep = decoded.indexOf(":");
  if (sep < 0) return false;
  const user = decoded.slice(0, sep);
  const password = decoded.slice(sep + 1);

  // Both comparisons run before the && so a wrong username costs the same as a wrong
  // password.
  const userOk = timingSafeEqual(user, expectedUser);
  const passwordOk = timingSafeEqual(password, expectedPassword);
  return userOk && passwordOk;
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
