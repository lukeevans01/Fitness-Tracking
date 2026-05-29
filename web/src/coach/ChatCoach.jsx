import { useEffect, useRef, useState } from "react";

const SUGGESTIONS = [
  "How do I fix my easy-pace drift?",
  "What should my long run build to?",
  "Should I add a quality session this week?",
];

// Send a compact slice of the dashboard data as context so the coach grounds its answers
// in real numbers without us shipping the whole dataset on every turn.
function buildContext(data) {
  if (!data) return undefined;
  const recent_weeks = (data.weekly || []).slice(-12).map((w) => ({
    week_start: w.week_start,
    km: w.km,
  }));
  return { recent: data.recent, recent_weeks };
}

function Bubble({ role, text }) {
  return (
    <div className={`bubble bubble-${role}`}>
      {text.split("\n\n").map((para, i) => (
        <p key={i}>{para}</p>
      ))}
    </div>
  );
}

export default function ChatCoach({ data }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, busy]);

  async function ask(question) {
    const q = question.trim();
    if (!q || busy) return;
    setError(null);
    const history = [...messages, { role: "user", text: q }];
    setMessages(history);
    setInput("");
    setBusy(true);
    try {
      const res = await fetch("/api/coach", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages: history, context: buildContext(data) }),
      });
      const raw = await res.text();
      let body;
      try {
        body = JSON.parse(raw);
      } catch {
        // A non-JSON response usually means the request did not reach the coach
        // function (for example an edge or deploy blip serving the SPA shell).
        throw new Error("the coach is temporarily unavailable, please try again in a moment");
      }
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      setMessages((m) => [...m, { role: "coach", text: body.answer }]);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e) {
    e.preventDefault();
    ask(input);
  }

  return (
    <div className="card chat">
      <div className="chat-head">
        <h2>Ask your running coach</h2>
        <span className="sub">Advice grounded in your data. Plan changes still go through your daily emails.</span>
      </div>

      <div className="chat-log" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <p>Ask anything about your running. For example:</p>
            <div className="chat-suggest">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" onClick={() => ask(s)} disabled={busy}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <Bubble key={i} role={m.role} text={m.text} />
        ))}
        {busy && <div className="bubble bubble-coach typing">Thinking…</div>}
        {error && <div className="chat-error">Coach unavailable: {error}</div>}
      </div>

      <form className="chat-input" onSubmit={onSubmit}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about pacing, long runs, recovery…"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
