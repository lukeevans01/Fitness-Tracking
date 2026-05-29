# Handover — fitness email coaching system

This document is the complete context for picking up work on Luke Evans's personal fitness coaching system. Read it end-to-end before making changes. It assumes no prior conversation history.

**Repo:** https://github.com/lukeevans01/Fitness-Tracking (private)

**Status as of 2026-05-26: fully live.** Daily 19:00 emails, Sunday weekly summaries, and the feedback loop all run on GitHub Actions cron. No machine needs to be running. Cost: £0/month.

---

## 1. The goal

The system does five things:

1. Sends Luke an email at **19:00 Amsterdam time every evening** with tomorrow's recommended workout — full session details (exercises, sets, reps, weights, rest times for strength; pace, distance, HR targets for runs).
2. Sends a **Sunday 08:00 weekly summary** with a review of last week and three plan options for the coming week (A/B/C), with a recommendation.
3. Lets Luke **reply with A, B, or C** to lock in the week plan. That choice is stored and passed to Gemini as context when adjusting individual sessions.
4. Lets Luke **reply to any session email in natural language** to adjust the plan. Gemini revises the session and sends a replacement within ~30 minutes.
5. Handles **survival mode** — when the baby is born or Luke is otherwise unable to train, one reply pauses all coaching emails. Resume with "I'm back".

Whatever's most recent in `overrides.json` is what Luke does at 06:00 the next morning.

---

## 2. Luke's situation

Luke is an amateur marathoner aiming for **sub-3:25 at San Sebastián marathon on 22 November 2026**. His current PB is 3:28:58 (Nice-Cannes, Nov 2025). His first baby was born ~late May 2026.

**Key facts Gemini always gets:**
- 32 years old, 4+ years strength training, 8 years Strava history
- Squash Tuesday evenings (treated as intensity — not added on top of run days)
- Sleep deprivation ongoing from late May 2026
- Hard rules: sleep <6h → short version or skip; no PBs in training; if Luke says wrecked, believe him

**Critical training insight:** Luke's natural pace drifts into the grey zone — self-described easy runs were 5:30–5:45/km at HR 155–170 in 2025 (86% of km were moderate, 0% truly easy). Sub-3:25 hinges on fixing this. The system specifies easy runs at HR <150 and 5:35–6:00/km, deliberately slower than what feels right.

---

## 3. File structure

```
fitness-emails/
├── send_daily.py              # Builds + sends tomorrow's session at 19:00
├── send_sunday.py             # Sunday 08:00 weekly summary with A/B/C options
├── process_replies.py         # Polls Gmail IMAP, routes all replies, calls Gemini
├── gemini_client.py           # Gemini 2.5 Flash REST wrapper (curl via subprocess)
├── coach_orchestrator.py      # Routes sessions to specialists; taper detection
├── training_summary.py        # strava.csv + strong.csv → compact text for Gemini
├── plan_template.json         # All session data: repeating 7-day training cycle
├── state.json                 # mode, week_choice, week_choice_label
├── overrides.json             # Per-date session overrides. Auto-cleaned >7 days.
├── feedback_log.jsonl         # Append-only log of all feedback received
├── adaptation_state.md        # Human-readable state: mode, phase, weekly counters, taper
├── race_calendar.md           # Race schedule: San Sebastián 22 Nov 2026, tune-ups
├── muscle_taxonomy.md         # Muscle groups mapped to plan exercises
├── specialists/
│   ├── __init__.py
│   ├── running.py             # Running coaching context for Gemini
│   ├── lifting.py             # Strength coaching context for Gemini
│   ├── mobility.py            # Rest/mobility coaching context for Gemini
│   └── nutrition.py           # Nutrition coaching context for Gemini
├── data/
│   ├── strava.csv             # Luke overwrites weekly (Strava full-history export)
│   ├── strong.csv             # Luke overwrites weekly (Strong export)
│   └── food_lookup_cache.json # Open Food Facts cache (auto-populated)
├── plans/
│   ├── current-week.md        # Coming week sessions (auto-written each Sunday)
│   └── pending-choice.json    # A/B/C options from latest Sunday summary
├── nutrition_log/.gitkeep     # Directory for future nutrition tracking
├── mobility_log/.gitkeep      # Directory for future mobility tracking
└── .github/workflows/
    ├── daily-email.yml        # Cron: 17:00 + 18:00 UTC (= 19:00 Amsterdam, DST-safe)
    ├── sunday-reminder.yml    # Cron: 06:00 + 07:00 UTC Sundays; commits plan files
    └── process-replies.yml    # Cron: every 30 min, 24/7; commits state files
```

---

## 4. How the system works end-to-end

### 4.1 Evening email (19:00)

`daily-email.yml` → `send_daily.py` → builds tomorrow's session from `plan_template.json` (checking `overrides.json` first) → sends via Resend with Reply-To pointing to bot Gmail.

### 4.2 Sunday summary (08:00)

`sunday-reminder.yml` → `send_sunday.py`:
1. Reads `adaptation_state.md`, skips if mode is survival/paused
2. Calls `training_summary.build_stats(days=7)` → syncs weekly counters into `adaptation_state.md`
3. Calls `_compute_standard_week()` → writes `plans/current-week.md`
4. Calls `coach_orchestrator.generate_weekly_summary()` via Gemini → three week options (A/B/C)
5. Sends email with option cards, recommended one highlighted
6. Saves `plans/pending-choice.json` with the full A/B/C options + expiry
7. Commits `plans/pending-choice.json`, `plans/current-week.md`, `adaptation_state.md` back to repo

### 4.3 Reply handling (every 30 min)

`process-replies.yml` → `process_replies.py`:

| Reply contains | Action |
|---|---|
| `survival mode` / `baby born` / `baby arrived` / `pause training` | Activates survival mode — daily emails stop |
| `I'm back` / `resume training` | Exits survival mode — daily emails resume |
| `pause` (exact) | Pauses all emails |
| `A`, `B`, or `C` (only) | Saves week plan choice to `state.json` |
| `revert` | Removes tomorrow's override; restores template session |
| Natural language feedback | Gemini revises tomorrow's session; `[Updated]` email sent |

The week choice (`state["week_choice"]`) is passed as context to Gemini on every subsequent session adjustment, so it knows the broader plan for the week.

### 4.4 Gemini routing

`coach_orchestrator.generate_session()` routes to the right specialist based on `session_kind`:
- `strength` → `specialists/lifting.py`
- `run` → `specialists/running.py`
- `rest` → `specialists/mobility.py`
- `nutrition` (explicit, not from session_kind) → `specialists/nutrition.py`

Each specialist provides a `system_context()` string injected into the prompt. Nutrition replies also trigger Open Food Facts lookup for any food words mentioned (`specialists/nutrition_lookup.py`).

### 4.5 Taper detection

`coach_orchestrator` auto-detects when within 28 days of race day (22 Nov 2026). Once active, a prescriptive taper block is injected into every Gemini prompt:
- No volume increases; cap at 70% of standard
- Strength: RIR 4, 2–3 sets/compound, skip accessories
- Running: easy only (HR <150, 5:35–6:00/km); one 15–20 min MP segment/week allowed
- Race week (≤7 days): easy runs 20–30 min max; no strength; full rest day before race

`sync_taper_state()` updates `adaptation_state.md` taper fields and is called at the start of both `send_sunday.py` and `process_replies.py`.

---

## 5. Key code patterns

**Resend + Gemini via curl, not requests/urllib.** Cloudflare blocks Python urllib's user-agent (HTTP 403). All HTTP is via `subprocess.run(["curl", ...])`. Do not replace with `requests`.

**Empty env vars treated as missing.** Always use `os.environ.get(KEY) or "default"`, never `os.environ.get(KEY, "default")`. GitHub Actions injects `""` for unset variables; `or` handles this.

**`target_date = today + 1`** in both `send_daily.py` and `process_replies.py`. Evening email previews tomorrow. Replies always adjust the day after today.

**Multiple replies chain.** Each Gemini call receives the prior override as `previous_override`. Latest override always wins.

**Gemini model.** `gemini-2.5-flash` via Google AI Studio free tier. Override via `GEMINI_MODEL` GitHub Actions variable (no code deploy needed).

**Python f-strings <3.12.** GitHub Actions uses Python 3.11. Backslashes inside f-string expressions are a syntax error — extract to variables first.

---

## 6. Survival mode

A single pause switch that replaces the old multi-stage training model. Much simpler.

**Enter:** Reply with `baby born`, `survival mode`, `baby arrived`, or `pause training`. Or edit `state.json` directly.

**Effect:** `state["mode"]` → `"survival"`, `cycle_state` → `"paused"`. Daily emails stop. Weekly Sunday summary continues (so Luke stays aware of the plan). Survival mode log entry written to `adaptation_state.md`.

**Exit:** Reply with `I'm back` or `resume training`. Training picks up immediately from the 7-day cycle. Goal and race date are unchanged.

**Pause everything:** Reply with exactly `pause`. Both daily and Sunday emails stop.

---

## 7. Training data

`training_summary.py` reads `data/strava.csv` and `data/strong.csv`:
- `build_summary(days=14)` → ~800-token text block for Gemini (runs + key lifts)
- `build_stats(days=7)` → `{run_sessions, run_km_total, strength_sessions}` for adaptation_state.md

Luke overwrites these weekly per the Sunday prompt: export from Strava (full history → strava.csv) and Strong (strong.csv), commit and push.

---

## 8. state.json fields

| Field | Purpose |
|---|---|
| `mode` | `normal` / `survival` / `paused` |
| `cycle_state` | `active` / `paused` — kept in sync for `send_daily.py` |
| `baby_birth_date` | Set when survival mode entered |
| `week_choice` | Sessions text from Luke's A/B/C pick (passed to Gemini as week context) |
| `week_choice_label` | Human label e.g. "Option B — Recovery focus" |

---

## 9. Secrets and variables

| Secret | Purpose |
|---|---|
| `RESEND_API_KEY` | Resend email API |
| `GMAIL_USER` | Bot Gmail address (IMAP + Reply-To) |
| `GMAIL_APP_PASSWORD` | 16-char App Password |
| `GEMINI_API_KEY` | Google AI Studio key |

| Variable | Default | Purpose |
|---|---|---|
| `TO_EMAIL` | `levans092@gmail.com` | Recipient |
| `FROM_EMAIL` | `Luke's Fitness Bot <onboarding@resend.dev>` | Sender display name |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Override without code deploy |

---

## 10. Workflow commit strategy

| Workflow | Files committed |
|---|---|
| `sunday-reminder.yml` | `plans/pending-choice.json`, `plans/current-week.md`, `adaptation_state.md` |
| `process-replies.yml` | `overrides.json`, `feedback_log.jsonl`, `state.json`, `adaptation_state.md`, `plans/pending-choice.json` |

Always `git pull --rebase` before pushing manual fixes — the bot commits frequently.

---

## 11. Quick reference — 7-day training cycle (start: 2026-05-25)

| Day | Session | Kind | Duration |
|---|---|---|---|
| 1 | Strength A1 — Lower, squat-focused | strength | 60 min |
| 2 | Easy run 6–8 km + optional squash | run | 40 min |
| 3 | Strength A2 — Upper, push-focused | strength | 60 min |
| 4 | Easy run 6–8 km | run | 40 min |
| 5 | Rest / optional mobility | rest | 20 min |
| 6 | Easy long run 12–14 km | run | 75 min |
| 7 | Rest | rest | — |
| 8 | Strength A3 — Full body athletic, lighter | strength | 50 min |
| 9 | Easy run 6 km + optional squash | run | 35 min |
| 10 | Strength B1 — Lower, hinge-focused | strength | 60 min |

**Pace zones (sub-3:25):**
- Easy/recovery: 5:35–6:00/km, HR <150
- Long run: 5:25–5:45/km, HR 150–160
- Marathon pace: 4:51/km, HR 165–170
- Threshold: 4:30–4:40/km, HR 172–178
- VO2/5k: 4:00–4:15/km, HR 180+

---

## 12. Gotchas

- **Cloudflare + Resend.** Always use curl, never Python HTTP libs.
- **Empty env vars.** Use `os.environ.get(KEY) or "default"` not `get(KEY, "default")`.
- **Backslashes in f-strings.** Python 3.11 — extract to variable first.
- **Taper window.** Starts 28 days before race (25 Oct 2026). Prompt caps are prescriptive, not advisory.
- **A/B/C reply detection.** `_RE_WEEK_CHOICE` matches exactly one letter, optional punctuation, nothing else. A reply of just "A sounds good" does NOT match — it's treated as natural language feedback.
- **Week choice expiry.** `plans/pending-choice.json` has an `expires` field (7 days after week_start). Stale picks are ignored.
- **Race day:** 22 Nov 2026. The marathon build should ramp up ~4 Aug 2026 (14 weeks out), driven by the progression engine (planned in a later pack).

---

End of handover. The repo is ground truth. If this doc conflicts with the code, trust the code and update this doc.
