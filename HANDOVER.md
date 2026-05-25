# Handover — fitness email coaching system

This document is the complete context for picking up work on Luke Evans's personal fitness coaching system. Read it end-to-end before making changes. It assumes no prior conversation history.

**Repo:** https://github.com/lukeevans01/Fitness-Tracking (private)

**Status as of 2026-05-25: fully live.** Daily 19:00 evening emails + feedback loop both running on GitHub Actions cron. No machine needs to be running. Cost: £0/month.

---

## 1. The goal

The system does three things:

1. Sends Luke an email at **19:00 Amsterdam time every evening** with tomorrow's recommended workout — full session details (exercises, sets, reps, weights, rest times for strength; pace, distance, HR targets for runs).
2. Lets Luke **reply to that email in natural language** ("swap to full body", "I'm wrecked, make it shorter", "make this a run not a lift") to adjust the plan.
3. Processes the reply via the **Google Gemini API** (free tier), regenerates the session with the feedback baked in, and sends a **replacement email** within ~30 minutes.

Whatever's most recent in `overrides.json` is what Luke does at 06:00 the next morning.

The system runs entirely on free infrastructure: GitHub Actions cron, Resend free tier, Gemini free tier, dedicated free Gmail account.

---

## 2. Why this exists — Luke's situation

Luke is an amateur marathoner aiming for **sub-3:25 at San Sebastián marathon on 22 November 2026**. His current PB is 3:28:58 (Nice-Cannes, Nov 2025). His first baby was due 25 May 2026. The fitness plan phases around the baby:

- **Phase 1** (pre-birth): pre-baby maintenance. 10-day rolling template.
- **Phase 2** (birth → ~6 weeks postpartum): no calendar — menu of opportunistic sessions. Daily email pauses to a weekly Monday digest. Feedback loop disabled.
- **Phase 3** (~6 weeks postpartum → race): 17-week structured marathon build. Not yet written.

Luke trains in the mornings (typically 06:00–11:00). Plays squash Tuesday evenings. Has 4+ years of data in the Strong app and 8 years on Strava — both CSVs are in the workspace.

**Critical training insight from the Phase 0 analysis:** Luke's natural pace drifts into the "grey zone" — his self-described easy runs are 5:30-5:45/km at HR 155-170, which is moderate, not easy. In his 2025 marathon block, 86% of km were moderate and 0% were truly easy. The sub-3:25 target hinges on fixing this. The system specifies easy runs at HR <150 and 5:35-6:00/km pace, deliberately slower than what feels right.

---

## 3. Current state of the repo

### 3.1 What is live

Everything is built and verified end-to-end as of 2026-05-25:

- **19:00 evening email** fires daily, previewing tomorrow's Phase 1 session. Reply-To is set to the bot Gmail so replies route automatically.
- **Feedback loop** polls Gmail every 30 minutes, 24/7. Replies trigger a Gemini call; a `[Updated]` replacement email arrives within ~30 minutes. Multiple replies in one evening are supported — latest override always wins, with the prior override passed to Gemini as context.
- **Commit-back** — `process-replies.yml` commits `overrides.json`, `feedback_log.jsonl`, and `state.json` back to the repo after each run so state persists across ephemeral Actions runners.
- **Sunday 18:00 reminder** asks for data refresh and weekly feedback.
- All three GitHub Actions workflows are live and cron-scheduled.

### 3.2 File structure

```
Fitness-Tracking/
├── send_daily.py              # Builds + sends tomorrow's session preview at 19:00
├── send_sunday.py             # Sunday 18:00 data-refresh reminder
├── process_replies.py         # Polls Gmail IMAP, calls Gemini, sends replacement emails
├── gemini_client.py           # Gemini 2.5 Flash REST wrapper (curl via subprocess)
├── training_summary.py        # Reads data/strava.csv + data/strong.csv → compact text for Gemini
├── plan_template.json         # All session data: Phase 1 10-day cycle, Phase 2 menu, Phase 3 placeholder
├── state.json                 # current_phase, baby_birth_date, phase3_start_date
├── overrides.json             # Per-date session overrides from feedback. Auto-cleaned >7 days old.
├── feedback_log.jsonl         # Append-only log of all feedback received
├── data/
│   ├── strava.csv             # Luke overwrites weekly (Strava full-history export)
│   └── strong.csv             # Luke overwrites weekly (Strong export)
├── .github/workflows/
│   ├── daily-email.yml        # Cron: 17:00 + 18:00 UTC (= 19:00 Amsterdam, DST-safe)
│   ├── sunday-reminder.yml    # Cron: 16:00 + 17:00 UTC Sundays
│   └── process-replies.yml    # Cron: every 30 min, 24/7
├── README.md
└── HANDOVER.md
```

### 3.3 Behaviour and known quirks

**Resend integration via curl, not requests/urllib.** Resend's API sits behind Cloudflare, which blocks Python urllib's default user-agent (HTTP 403 error 1010). All scripts shell out to curl via `subprocess.run`. **Do not** replace this with `requests` or `urllib`. Python is used only to build the JSON payload safely.

**Gemini integration also via curl.** Same pattern for consistency, even though Gemini is not behind Cloudflare.

**Empty env vars treated as missing.** Always use `os.environ.get(KEY) or "default"`, never `os.environ.get(KEY, "default")`. GitHub Actions injects an empty string for unset `${{ vars.X }}` variables; the `or` pattern handles this correctly.

**Time gate on `send_daily.py` (19:00 ±30 min), bypassed on `workflow_dispatch`.** `process_replies.py` has no time gate — it polls 24/7 so replies land any time Luke wants to adjust, including after his morning session.

**DST handling on daily-email and sunday-reminder.** Both fire at two UTC times (CEST and CET equivalent). The local-time gate in the script decides which one actually runs.

**`target_date = today + 1 day` in `send_daily.py`.** The evening email always previews tomorrow's session. The day-after-tomorrow is shown as a preview within that email.

**`process_replies.py` always targets tomorrow.** Replies affect the day after today, regardless of time of day. A reply at 09:00 on Tuesday updates Wednesday's session. This is correct — by the time Luke replies after his morning session, today's session is already done.

**Phase 2 feedback loop.** Phase 2 disables training feedback (no daily sessions to adjust), but phase-transition commands ("switch to phase 3", "I'm ready") still work via email reply.

**Gemini model.** `gemini-1.5-flash` and `gemini-2.0-flash` are both unavailable to new Google AI Studio accounts. The model is `gemini-2.5-flash`. It's configurable via `GEMINI_MODEL` env var (GitHub Actions variable) if it needs changing again without a code deploy.

**Polling cost budget.** 30-min intervals 24/7 = ~1,440 GitHub Actions minutes/month. Free tier is 2,000 min/month for private repos. Current headroom: ~560 min/month.

### 3.4 Secrets and credentials

All set in the GitHub repo (Settings → Secrets and variables → Actions):

| Secret | Purpose |
|---|---|
| `RESEND_API_KEY` | Resend email sending API |
| `GMAIL_USER` | Bot Gmail address (for IMAP polling + Reply-To header) |
| `GMAIL_APP_PASSWORD` | 16-char App Password for IMAP login |
| `GEMINI_API_KEY` | Gemini API key from Google AI Studio |

The Resend key is also stored locally at `/Users/luke.evans/Documents/Claude/Projects/Fitness App/.secrets/resend_api_key.txt` (gitignored). Keep in sync if rotated.

---

## 4. How the feedback loop works

```
19:00 Amsterdam
┌─────────────────────┐
│ daily-email.yml     │  Builds tomorrow's session from plan_template.json
│ (GitHub Actions)    │  Checks overrides.json first — uses override if present
└──────────┬──────────┘  Sets Reply-To: bot Gmail
           │
           ▼
   levans092@gmail.com
           │
           │ Luke replies in natural language
           ▼
   bot Gmail (IMAP)
           │
           │ process-replies.yml polls every 30 min, 24/7
           ▼
┌─────────────────────────┐
│ process_replies.py      │
│ - strips quoted history │
│ - checks for special    │
│   commands (revert,     │
│   phase transitions)    │
│ - calls Gemini 2.5 Flash│
│ - validates JSON schema │
│ - writes overrides.json │
│ - appends feedback_log  │
│ - sends [Updated] email │
│ - commits files back    │
└─────────────────────────┘
```

**Special reply commands:**

| Reply contains | Action |
|---|---|
| Natural language feedback | Gemini revises session; `[Updated]` email sent |
| `revert` | Deletes override for tomorrow; sends original template session |
| `switch to phase 2` / `baby born` | Transitions to Phase 2, sets `baby_birth_date` to today |
| `switch to phase 3` / `I'm ready` | Transitions to Phase 3 |
| `pause` | Sets phase to `paused` |

Phase transitions commit `state.json` back to the repo automatically.

---

## 5. Decisions made

| Decision | What was implemented | Why |
|---|---|---|
| Inbound email | Dedicated Gmail + IMAP polling | Free, no domain needed |
| LLM | Gemini 2.5 Flash via Google AI Studio | Free tier, capable enough; 1.5 and 2.0 Flash unavailable for new accounts |
| Generation model | Hybrid — template default, LLM only on feedback | Predictable plan + minimal LLM cost |
| Multiple replies | Latest override always wins; prior override passed to Gemini as context | Lets Luke refine iteratively |
| Replacement email | Includes `coach_note` from Gemini at top | Essential UX — Luke sees what changed and why |
| Revert | Reply `revert` deletes override and sends original | Simple escape hatch |
| Polling window | 24/7 at 30-min intervals (no time gate) | Luke may reply after morning session to adjust the next day |
| Polling interval | 30 min (not 15 min) | 15 min × 24h exceeds GitHub free-tier minute budget |
| Adjustment scope | Just that day (no auto-rebalance of week) | Simplest mental model |
| Override TTL | Auto-clean entries older than 7 days | Prevents stale data accumulation |
| Phase 2 feedback | Disabled for training; phase transitions still work | No daily sessions to adjust in Phase 2 |
| Phase 3 feedback | Same architecture as Phase 1 | Full feedback loop when marathon build is active |
| Failure mode | Log error, skip replacement, send plain notice to Luke | Don't silently fail; let Luke decide whether to retry |
| Outbound from | `Luke's Fitness Bot <onboarding@resend.dev>` | Already works; domain verification is future work |
| Reply-To header | Set to bot Gmail on all outbound emails | Replies route automatically |

---

## 6. Phase transitions

### Phase 1 → Phase 2 (when baby arrives)

**Option A — email reply:** Reply to any fitness email with `baby born` or `switch to phase 2`. The bot sets `baby_birth_date` to today, commits `state.json`, and confirms by email.

**Option B — direct edit:** Edit `state.json`, commit, push.

```json
{
  "current_phase": "phase2",
  "baby_birth_date": "2026-05-27",
  "phase3_start_date": null
}
```

Effect: daily emails stop. Monday-only weekly digest begins.

### Phase 2 → Phase 3 (when ready for marathon build)

**Option A — email reply:** Reply with `I'm ready` or `switch to phase 3`.

**Option B — direct edit:** Set `current_phase` to `phase3` and `phase3_start_date` to the start date. Then open Cowork and say "build Phase 3" — Claude will write the 17-week block into `plan_template.json["phase3"]["weeks"]`.

Phase 3 needs ~14 weeks minimum before race day (22 Nov 2026). Target start: ~4 Aug 2026.

---

## 7. Training data for Gemini

`training_summary.py` reads `data/strava.csv` and `data/strong.csv` and produces a compact ~800-token summary of the last 14 days. Gemini receives this as context when generating overrides.

Luke overwrites these files weekly per the Sunday reminder:
1. Export full history from Strava → save as `data/strava.csv`
2. Export from Strong → save as `data/strong.csv`
3. Commit and push

If the files are absent, `training_summary.py` returns a graceful "no data available" message and Gemini still works — it just has no recent training context.

---

## 8. The Gemini prompt

Full prompt is in `gemini_client.py` (`_COACH_CONTEXT` + `_SESSION_SCHEMA`). Key points:

- Coach persona with Luke's full profile, benchmarks, marathon target
- Hard training principles baked in (80/20 polarised, HR <150 for easy runs, RIR 3, no PBs)
- Current session JSON + optional prior override + 14-day training summary + Luke's reply
- `responseMimeType: application/json` forces structured output
- Response validated against required keys before applying
- If feedback is unclear or unsafe, Gemini returns the original session unchanged with explanation in `coach_note`

Tone instruction (direct, no motivational fluff) is in the prompt.

---

## 9. Testing — what was verified

End-to-end test completed 2026-05-25:

1. ✅ `daily-email.yml` manual trigger → email arrived at `levans092@gmail.com` with tomorrow's session, Reply-To set to bot Gmail
2. ✅ Reply sent → `process-replies.yml` manual trigger → Gemini called → `[Updated]` replacement email arrived
3. ✅ Second reply to `[Updated]` email → override refined again (multiple-reply chaining works)
4. ✅ `overrides.json` committed back to repo by `fitness-bot`
5. ✅ Cron live for all three workflows

---

## 10. Setup checklist — all complete

- [x] Private GitHub repo at `lukeevans01/Fitness-Tracking`
- [x] `RESEND_API_KEY` secret added
- [x] Test workflow verified email delivery to `levans092@gmail.com`
- [x] Cowork scheduled tasks disabled (no duplicate sends)
- [x] Dedicated Gmail bot account created with 2FA + App Password
- [x] Gemini API key created at aistudio.google.com
- [x] `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `GEMINI_API_KEY` secrets added
- [x] Section 7 implemented and pushed
- [x] End-to-end test passed
- [x] Cron running

---

## 11. Quick reference — Luke's plan structure

**Phase 1 rolling 10-day cycle** (start: 2026-05-25):
- Day 1: Strength A1 (Lower, squat-focused, 60 min)
- Day 2: Easy run 6-8 km + optional squash
- Day 3: Strength A2 (Upper, push-focused, 60 min)
- Day 4: Easy run 6-8 km
- Day 5: Rest / optional mobility
- Day 6: Easy long run 12-14 km
- Day 7: Rest
- Day 8: Strength A3 (Full body athletic, 50 min, lighter)
- Day 9: Easy run 6 km + optional squash
- Day 10: Strength B1 (Lower, hinge-focused, 60 min)

**Hard rules (every Phase 1 email):**
- Easy means easy — HR <150 on easy runs.
- Sleep is the override: <6h = drop to a walk or skip.
- No PB attempts.
- If labour starts, stop.

**Pace targets (sub-3:25 marathon):**
- Easy/recovery: 5:35-6:00/km, HR <150
- Long run base: 5:25-5:45/km, HR 150-160
- Marathon pace: 4:51/km, HR 165-170
- Threshold: 4:30-4:40/km, HR 172-178
- 5k/VO2 intervals: 4:00-4:15/km, HR 180+

---

## 12. Tone for emails

Luke prefers direct, concise, no motivational fluff. Baked into the Gemini system prompt:

> Output tone: direct and concise. No "you've got this!" / "let's crush it!" /
> motivational language. Treat Luke as a competent adult who has been training
> for 8 years. Explain *why* in one sentence. Move on.

---

## 13. Code gotchas

- **Cloudflare blocks Python urllib's user-agent.** Use curl for all HTTP. Applies to Resend; Gemini is fine but use curl anyway for consistency.
- **`os.environ.get(KEY, default)` returns `""` not `default` when var is set to empty string.** GitHub Actions sets `${{ vars.X }}` as `""` when the variable doesn't exist. Always use `os.environ.get(KEY) or "default"`.
- **Python f-strings <3.12 reject backslashes inside expressions.** GitHub Actions uses Python 3.11. Extract variables before the f-string.
- **Time gate on `send_daily.py` must allow manual bypass.** `GITHUB_EVENT_NAME == "workflow_dispatch"` skips the gate. Don't remove this.
- **GitHub Actions cron has 5-15 min drift.** The ±30 min gate on `send_daily.py` accommodates this.
- **DST handling:** daily-email and sunday-reminder fire at two UTC times; the local-time gate decides which one runs. Don't encode DST in the cron.
- **Gemini model deprecation.** `gemini-1.5-flash` and `gemini-2.0-flash` are unavailable to new accounts. Current model: `gemini-2.5-flash`. Override via `GEMINI_MODEL` GitHub Actions variable without a code deploy.
- **process-replies.yml commits back.** The workflow commits `overrides.json`, `feedback_log.jsonl`, and `state.json` after each run. This means the remote can be ahead of local when pushing fixes. Always `git pull --rebase` before pushing.

---

End of handover. The repo is ground truth. If this doc conflicts with what's actually in the repo, trust the repo and update this doc.
