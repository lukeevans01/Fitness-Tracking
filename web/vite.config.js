import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { callGemini, stubAnswer } from "./functions/api/_coach.js";

// Dev-only shim for POST /api/coach. In production this route is served by the Cloudflare
// Pages Function at functions/api/coach.js; the Vite dev server does not run that, so we
// reproduce it here against the same shared module. If GEMINI_API_KEY is set in the local
// environment we call the live coach, otherwise we return a deterministic stub so the chat
// UI is still demonstrable.
function coachDevApi() {
  return {
    name: "coach-dev-api",
    configureServer(server) {
      server.middlewares.use("/api/coach", async (req, res, next) => {
        if (req.method !== "POST") return next();
        let raw = "";
        req.on("data", (c) => (raw += c));
        req.on("end", async () => {
          const send = (obj, status = 200) => {
            res.statusCode = status;
            res.setHeader("content-type", "application/json");
            res.end(JSON.stringify(obj));
          };
          let payload;
          try {
            payload = JSON.parse(raw || "{}");
          } catch {
            return send({ error: "Invalid JSON body" }, 400);
          }
          const messages = Array.isArray(payload.messages) ? payload.messages : [];
          if (messages.length === 0) {
            return send({ error: "messages array is required" }, 400);
          }
          const apiKey = process.env.GEMINI_API_KEY;
          try {
            const answer = apiKey
              ? await callGemini(apiKey, messages, payload.context)
              : stubAnswer(messages, payload.context);
            send({ answer });
          } catch (err) {
            send({ error: `Coach call failed: ${err.message}` }, 502);
          }
        });
      });
    },
  };
}

// Deployed at the domain root on Cloudflare Pages, so base "/".
export default defineConfig({
  plugins: [react(), coachDevApi()],
  base: "/",
});
