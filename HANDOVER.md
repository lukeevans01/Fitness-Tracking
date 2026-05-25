# Handover — fitness email coaching system

This document is the complete context for picking up work on Luke Evans's personal fitness coaching system. Read it end-to-end before making changes. It assumes no prior conversation history.

---

## 1. The goal

Build a fitness coaching system that:

1. Sends Luke an email at **19:00 Amsterdam time every evening** with tomorrow's recommended workout — full session details (exercises, sets, reps, weights, rest times for strength; pace, distance, HR targets for runs).
2. Lets Luke **reply to that email in natural language** ("swap to full body", "I'm wrecked, make it shorter", "make this a run not a lift") to adjust the plan.
3. Processes the reply via the **Google Gemini API** (free tier), regenerates the session with the feedback baked in, and sends a **replacement email** within ~15 minutes.
4. Whatever's most recent is what Luke does at 06:00 the next morning. No "deadline" beyond that.

The system runs entirely on free infrastructure (GitHub Actions cron, Resend free tier, Gemini free tier, dedicated free Gmail account). No machine needs to be running. Cost: £0/month.

---

## 2. Why this exists — Luke's situation

Luke is an amateur marathoner aiming for **sub-3:25 at San Sebastián marathon on 22 November 2026**. His current marathon PB is 3:28:58 (Nice-Cannes, Nov 2025). His first baby was due **25 May 2026** and may arrive up to 9 days late. The fitness plan therefore phases around the baby:

- **Phase 1** (today → birth): pre-baby maintenance. 10-day rolling template.
- **Phase 2** (birth → ~6 weeks postpartum): no calendar — menu of opportunistic sessions. Daily email pauses to a weekly Monday digest.
- **Phase 3** (~6 weeks postpartum → race): 17-week structured marathon build. Not yet written; will be built when Luke reports ready.

Luke trains in the mornings (typically before 07:00). He plays squash Tuesday evenings. He's been logging in the Strong app for 4+ years and on Strava for 8 years — both datasets are in the workspace as CSVs.

**Critical training insight from the Phase 0 analysis:** Luke's natural pace runs into the "grey zone" — his self-described easy runs are 5:30-5:45/km at HR 155-170, which is moderate not easy. In his 2025 marathon block, 86% of km were moderate, 0% were truly easy. The sub-3:25 target hinges on fixing this. All easy runs in this system specify HR <150 and 5:35-6:00/km pace, deliberately slower than what feels right.

---

## 3. What's already built

### 3.1 The plan deliverables (in `Fitness App/`)

These are Word docs the user already has; do not regenerate.

- `Phase 0 - Fitness Baseline Analysis.docx` — analysis of current fitness state vs goals
- `Phase 1 - Pre-Baby Maintenance Plan.docx` — 10-day rolling template with full session details
- `Phase 2 - Postpartum Recovery Menu.docx` — time × body-state grid
- `Full Body Workout.xlsx`, `hybrid-athlete-strength-program.md`, `strava activities.csv`, `strong_workouts.csv` — original input data

### 3.2 The email automation (in `Fitness App/fitness-emails/`)

This folder **IS the GitHub repo**. To deploy, Luke pushes its contents to a private GitHub repo and configures secrets.

```
fitness-emails/
├── plan_template.json          # All plan data — Phase 1 days, Phase 2 menu, Phase 3 placeholder
├── state.json                  # current_phase, baby_birth_date, phase3_start_date
├── send_daily.py               # Builds and sends today's email via Resend
├── send_sunday.py              # Sunday 18:00 data-refresh reminder
├── .github/workflows/
│   ├── daily-email.yml         # Cron: 03:30 + 04:30 UTC daily (DST-safe gate in script)
│   └── sunday-reminder.yml     # Cron: 16:00 + 17:00 UTC every Sunday
├── .gitignore                  # Excludes secrets, pycache, .DS_Store
└── README.md                   # Setup instructions for Luke
```

**Current behaviour:**
- `send_daily.py` reads `plan_template.json` + `state.json`, calculates today's "Day N" of the 10-day cycle (Phase 1 starts 2026-05-25), renders HTML + plain text email, POSTs to Resend API via curl.
- Cloudflare blocks Python urllib's user-agent (HTTP 403 error 1010), so the POST shells out to curl. **Important** — don't try to use Python's requests/urllib for the Resend API.
- Local-time gate: script reads Amsterdam tz via zoneinfo and exits if not within ±30 min of target time. This handles DST (we cron both UTC times that map to target Amsterdam time across DST boundaries; only one passes the gate).
- Phase 2: emails only send on Mondays.
- Phase 3: looks up by date in `phase3.weeks` (not yet populated).

**Resend setup:** Luke has a Resend API key stored at `Fitness App/.secrets/resend_api_key.txt` (NOT committed; the `.gitignore` excludes `.secrets/`). For deployment it's stored as a GitHub Actions secret `RESEND_API_KEY`.

Outbound `from` address: `Luke's Fitness Bot <onboarding@resend.dev>` (Resend's shared sender — sufficient for personal use). Recipient: `levans092@gmail.com`.

### 3.3 What works now

If Luke pushes the current folder to GitHub and adds the `RESEND_API_KEY` secret, he gets:
- A daily email at 05:30 Amsterdam time with tomorrow's session (deterministic from the template)
- A Sunday 18:00 reminder to refresh data
- Autonomous operation, no Cowork required

---

## 4. What needs to be built (this handover's scope)

### 4.1 Three new capabilities

1. **Shift daily send time to 19:00 evening-before** instead of 05:30 morning-of. The email is now a preview of tomorrow's session, not a reminder of today's.
2. **Inbound email handling.** Luke replies to the 19:00 email with natural-language adjustments. Replies are received in a dedicated Gmail account; a polling cron picks them up.
3. **LLM-driven plan adjustment.** A Gemini call parses the reply, considers the current plan + recent training data + Luke's goals + the marathon training context, and proposes an adjusted session for tomorrow. The system sends a replacement email and stores the adjustment.

### 4.2 Architectural changes

```
Current:                            New:

┌─────────────────────┐             ┌─────────────────────┐
│ GitHub Actions cron │             │ GitHub Actions cron │
│ 05:30 daily         │             │ 19:00 evening-before│
└──────────┬──────────┘             └──────────┬──────────┘
           │                                   │ (set Reply-To: gmail)
           ▼                                   ▼
       [Resend]                            [Resend]
           │                                   │
           ▼                                   ▼
   levans092@gmail.com               levans092@gmail.com
                                              │
                                              │ (reply with feedback)
                                              ▼
                                  ┌─────────────────────────┐
                                  │ luke.fitness.bot@gmail  │
                                  │ (dedicated mailbox)     │
                                  └────────────┬────────────┘
                                               │
                                               │ IMAP poll every 15 min
                                               │ (19:30 → 05:30)
                                               ▼
                                  ┌─────────────────────────┐
                                  │ process_replies.py      │
                                  │ - reads new replies     │
                                  │ - calls Gemini API      │
                                  │ - writes overrides.json │
                                  │ - sends replacement     │
                                  │   email via Resend      │
                                  └─────────────────────────┘
```

### 4.3 New data files

**`overrides.json`** — stores per-date session overrides applied by feedback processing.

```json
{
  "_comment": "Per-date session overrides from feedback replies. Auto-cleaned of entries older than 7 days on each run.",
  "overrides": {
    "2026-05-26": {
      "applied_at": "2026-05-25T19:42:15+02:00",
      "feedback_source": "reply: 'swap lower body for full body, I want a longer session'",
      "session": {
        "session_type": "Full body strength",
        "session_kind": "strength",
        "duration_min": 75,
        "warm_up": "...",
        "exercises": [...],
        "short_version": "...",
        "purpose": "..."
      }
    }
  }
}
```

`send_daily.py` checks `overrides.json` first; if there's an entry for tomorrow's date, it uses that session instead of the template lookup.

**`feedback_log.jsonl`** — append-only log of all feedback received. Each line is a JSON object: `{timestamp, message_id, from_address, reply_text, gemini_response, override_applied}`. Useful for the Sunday review and for future fine-tuning.

---

## 5. Decisions made (defaults to follow unless told otherwise)

| Decision | Default | Why |
|---|---|---|
| Inbound email | Dedicated Gmail + IMAP polling | Free, no domain needed, ~15 min latency acceptable |
| LLM | Gemini 1.5 Flash via Google AI Studio | Free tier 15 RPM / 1M tokens per day, capable enough, no Anthropic key needed |
| Generation model | Hybrid — template default, LLM only on feedback | Predictable plan + cheap LLM usage |
| Adjustment scope | Just that day (no auto-rebalance of week) | Simplest mental model |
| Polling window | 19:30 → 05:30 Amsterdam local, every 15 min | Covers evening reply window; daytime is for execution, not adjustment |
| Override TTL | Auto-clean entries older than 7 days | Prevents stale data accumulating |
| Outbound from | Keep `Luke's Fitness Bot <onboarding@resend.dev>` | Already works; switching to verified domain is optional future work |
| Reply-To header | Set to dedicated Gmail | Replies land in the inbound mailbox automatically |

---

## 6. Prerequisites Luke must complete before Claude Code work begins

### 6.1 Create a dedicated Gmail account

Sign up at gmail.com for a new account dedicated to this bot. Suggested name: `luke.fitness.bot@gmail.com` (or similar — must be unique).

- Enable 2-step verification on the account (required for App Passwords)
- Generate an **App Password** at https://myaccount.google.com/apppasswords — call it "fitness-bot-imap"
- Note the 16-character App Password (formatted xxxx xxxx xxxx xxxx) — copy it; you won't see it again

### 6.2 Get a Gemini API key

- Go to https://aistudio.google.com/app/apikey (sign in with any Google account, doesn't need to be the bot one)
- Click "Create API key" → "Create API key in new project"
- Copy the key

### 6.3 Add three new secrets to the GitHub repo

Settings → Secrets and variables → Actions → New repository secret:

- `GEMINI_API_KEY` = the API key from 6.2
- `GMAIL_USER` = the bot's email address (e.g., `luke.fitness.bot@gmail.com`)
- `GMAIL_APP_PASSWORD` = the 16-character App Password from 6.1 (no spaces)

`RESEND_API_KEY` is already set.

---

## 7. Implementation plan (for Claude Code)

### 7.1 Files to add

**`gemini_client.py`** — thin wrapper around the Gemini REST API. Used by `process_replies.py`. Single function: `generate_session(reply_text, current_session, recent_training_summary, plan_context) -> dict`. Returns a session dict in the same shape as entries in `plan_template.json["phase1_days"]`. Use curl via subprocess (same pattern as the Resend client) to avoid any urllib/requests issues; build the JSON payload in Python.

Gemini endpoint pattern (v1beta, with API key as query param):
```
POST https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=<KEY>
Content-Type: application/json

{
  "contents": [{"parts": [{"text": "<prompt>"}]}],
  "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}
}
```

Use `responseMimeType: application/json` to force structured output. Define a strict JSON schema in the prompt for the session shape. Validate the response against expected keys before applying; if invalid, log and don't override.

**`process_replies.py`** — the inbound polling script.

Pseudocode:
```python
1. Read env: GMAIL_USER, GMAIL_APP_PASSWORD, GEMINI_API_KEY, RESEND_API_KEY
2. Connect to imap.gmail.com:993, login, select INBOX
3. Search UNSEEN messages from levans092@gmail.com (Luke's personal — only process replies from him)
4. For each unread message:
   a. Extract Subject, From, plain text body (strip quoted history)
   b. Look up tomorrow's currently-scheduled session (template + any existing override)
   c. Summarise recent training (last 7 days from Strava CSV if present, last 14 days lifts from Strong CSV if present)
   d. Build Gemini prompt: system context (marathon training theory, current phase, Luke's profile), user reply, current session JSON, recent training summary
   e. Call gemini_client.generate_session()
   f. Validate response is a well-formed session dict
   g. Write to overrides.json under tomorrow's date
   h. Append entry to feedback_log.jsonl
   i. Render replacement email with the new session, prefix subject with "[Updated] "
   j. Send via Resend
   k. Mark IMAP message as Seen
5. Auto-clean overrides older than 7 days
6. Auto-clean feedback log entries older than 90 days (or keep all — small file)
7. Commit overrides.json and feedback_log.jsonl back to the repo via the GITHUB_TOKEN
```

The commit-back step matters: GitHub Actions runs ephemerally. To persist the override across the next morning's `send_daily.py` run, the workflow must commit the modified `overrides.json` (and `feedback_log.jsonl`) back to the repo. Use the built-in `GITHUB_TOKEN` and `git config user.name "fitness-bot" / git push` pattern.

**`.github/workflows/process-replies.yml`** — new workflow.

```yaml
name: Process feedback replies
on:
  schedule:
    # Every 15 min during 19:30 → 05:30 Amsterdam (CEST = UTC+2)
    # Cover both CEST and CET via overlapping windows; script gates on local time.
    # CEST: 17:30-03:30 UTC; CET: 18:30-04:30 UTC. Union: 17:30-04:30 UTC.
    - cron: '*/15 17-23 * * *'
    - cron: '*/15 0-4 * * *'
  workflow_dispatch: {}

jobs:
  poll-and-process:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: write   # Needed to commit overrides back
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Poll and process
        env:
          GMAIL_USER: ${{ secrets.GMAIL_USER }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          RESEND_API_KEY: ${{ secrets.RESEND_API_KEY }}
          TO_EMAIL: levans092@gmail.com
          FROM_EMAIL: "Luke's Fitness Bot <onboarding@resend.dev>"
        run: python3 process_replies.py
      - name: Commit overrides if changed
        run: |
          git config user.name "fitness-bot"
          git config user.email "bot@noreply"
          git add overrides.json feedback_log.jsonl
          git diff --cached --quiet || git commit -m "Apply feedback overrides ($(date -u +%FT%TZ))"
          git push
```

### 7.2 Files to modify

**`send_daily.py`** — three changes:

1. **Time gate** changes from 05:30 to 19:00 Amsterdam. Update `check_local_time_window()` target to `19 * 60 + 0`.
2. **"Today's" session becomes "tomorrow's" session.** The script now generates a preview for the day after the current calendar date. Change `today_local = datetime.now(TZ).date()` to compute `target_date = today_local + timedelta(days=1)` and use `target_date` everywhere downstream — including the date-modulo math, the subject line, and the "tomorrow preview" (which becomes "the day after tomorrow"). The day-cycle math becomes: `days_in = (target_date - phase1_start).days`.
3. **Override lookup.** Before the template lookup, check `overrides.json` for an entry matching `target_date.isoformat()`. If present, use that session directly; skip the template lookup but still build the email with the same renderer.
4. **Reply-To header.** Add `"reply_to": [os.environ.get("GMAIL_USER", "")]` to the Resend payload so replies route to the Gmail inbox automatically.

**`.github/workflows/daily-email.yml`** — change cron.

```yaml
# 19:00 Amsterdam = 17:00 UTC (CEST) or 18:00 UTC (CET). Both; script gates.
- cron: '0 17,18 * * *'
```

**`send_sunday.py`** — also add reply-to header for consistency. No other changes.

**`README.md`** — add a section on the new feedback loop:
- The new GitHub secrets to add
- How replies work
- Where the override/log files live
- How to reset (delete `overrides.json` to revert to template, etc.)

### 7.3 Files to verify don't break

- `state.json` schema stays identical; no migration needed.
- `plan_template.json` stays the source of truth for the default plan; overrides are layered on top per-day.

---

## 8. The Gemini prompt — design notes

The system prompt for Gemini should be carefully constructed. Below is a starting template that Claude Code should refine:

```
You are a strength-and-marathon coach for Luke Evans. Your job is to take his
feedback on tomorrow's scheduled session and return a revised session that
respects his training context.

Luke's profile:
- 32, amateur marathoner. Marathon PB 3:28:58 (Nov 2025). Target sub-3:25 for
  San Sebastián 22 Nov 2026.
- 4+ years consistent strength training, 2-3 sessions/week.
- Current strength benchmarks: Squat ~120kg e1RM, Bench ~85kg e1RM (target 96kg),
  RDL ~108kg e1RM (recent PB), OHP ~49kg e1RM. Excludes conventional deadlifts.
- Plays squash Tuesday evenings (treats as intensity).
- First baby due late May 2026 — sleep deprivation likely a major factor.

Training principles you MUST respect:
- 80/20 polarised distribution: easy must be truly easy (HR <150, 5:35-6:00/km).
  Luke's natural failure mode is running everything at moderate pace; resist this.
- Easy runs ≠ moderate runs. If Luke asks for an "easy" run, give him a slow one.
- Marathon-pace work is the highest-leverage running session (Pfitzinger principle).
- Strength: RIR 3 default. No PB attempts during marathon build.
- Compound movements before isolation. Bench is priority on push days
  (100kg trajectory).

Current scheduled session (for the date you are revising):
{SESSION_JSON}

Recent training (last 14 days):
{TRAINING_SUMMARY}

Luke's feedback message (his reply):
{REPLY_TEXT}

Output a revised session as JSON matching this exact schema:
{
  "session_type": str,
  "session_kind": "strength" | "run" | "rest",
  "duration_min": int,
  "warm_up": str,         // only for strength/run
  "exercises": [{"name", "sets_reps", "weight", "rest"}, ...],  // only for strength
  "run_details": {"pace", "hr_target", "duration", "distance", "effort"},  // only for run
  "details": str,         // only for rest
  "extras": str,          // optional
  "short_version": str,   // a tired/short-on-time fallback
  "purpose": str,         // one-sentence rationale for why this session
  "coach_note": str       // a brief explanation to Luke of what you changed and why
}

Only output the JSON. No surrounding text.
```

Key design choices:
- **Hard schema constraint** — using Gemini's `responseMimeType: application/json` plus an explicit schema in the prompt. Validate in Python before applying.
- **Training-principle guardrails** — the model must know Luke's grey-zone problem so it doesn't accidentally prescribe moderate easy runs.
- **Refuse / fallback path** — if Luke's reply is ambiguous or asks for something dangerous (e.g., "let's do 50 km today"), the prompt should explicitly allow the model to return the original session with a `coach_note` explaining why no change was made.

Add this clause:
```
If Luke's feedback is unclear, dangerous, or contradicts safe progression
(e.g., excessive volume jump, injury-risky combo), return the ORIGINAL session
unchanged and explain in coach_note. Do not adjust beyond reasonable bounds.
```

---

## 9. Training data access for the Gemini prompt

`process_replies.py` should build a `TRAINING_SUMMARY` from data Luke uploads to the repo. Two options:

**Option A — Static parsing from the existing CSVs**

The `strava activities.csv` and `strong_workouts.csv` in `Fitness App/` are already in the repo. But: they're NOT in the `fitness-emails/` subfolder, so they're not in the git repo by default. Two sub-options:

- **A1:** Move/copy them into `fitness-emails/data/` and let Luke overwrite weekly.
- **A2:** Don't include training data in Gemini prompt; let it work with just the scheduled session + feedback. Simpler but coach is less informed.

Recommend A1. Add `fitness-emails/data/{strava.csv, strong.csv}` and a simple parser that summarises last 14 days:
- Number of runs, total distance, pace distribution, longest run
- Number of strength sessions, top sets on bench/squat/RDL/OHP

**Option B — Strava + Strong APIs**

Strava has an OAuth API; Strong doesn't. Strava OAuth setup is heavy for this use case. Skip.

Recommendation: **Option A1.** Build a small `training_summary.py` that reads the CSVs and produces a compact text summary for Gemini.

---

## 10. Testing strategy

Before relying on this in production:

1. **Unit test the override flow.** Manually write an entry to `overrides.json` for tomorrow's date, run `send_daily.py`, verify the override is used.

2. **Unit test the Gemini client.** Hardcode a sample reply + current session + training summary, call Gemini, verify the response parses as valid JSON matching the schema.

3. **End-to-end dry run.** Send a test email manually (`gh workflow run daily-email.yml`). Reply to it from `levans092@gmail.com`. Trigger the polling workflow manually (`gh workflow run process-replies.yml`). Verify:
   - Polling found the reply
   - Gemini was called and returned valid JSON
   - `overrides.json` was updated and committed
   - Replacement email arrived

4. **Test the Sunday digest.** Trigger `sunday-reminder.yml` manually; verify content.

5. **Test phase transitions.** Edit `state.json` to `phase2`, trigger daily workflow on a non-Monday — should exit silently. Trigger on Monday — should send weekly digest.

---

## 11. Open questions / things Claude Code should ask Luke about

These were not resolved in the planning conversation:

1. **What should happen if Luke replies multiple times in one evening?** Suggested default: each reply triggers a new Gemini call with the cumulative feedback history (or just the latest); latest override always wins. The Gemini prompt could include "previous overrides for this date" as additional context.

2. **Should the replacement email mention what changed?** Suggested default: yes — include the `coach_note` from Gemini at the top of the email body, e.g., "Coach note: swapped lower-body for full-body as requested; kept the volume similar." This is essential UX.

3. **Should Luke be able to revert to the original?** Suggested: a reply containing the literal text "revert" deletes that date's override.

4. **Phase 2 / Phase 3 behaviour for feedback loop?**
   - Phase 2: probably leave the feedback loop disabled (only Monday digest). If Luke wants to adjust, he goes to Cowork.
   - Phase 3: feedback loop fully active. Same architecture as Phase 1.

5. **Failure modes — what if Gemini is down or rate-limited?**
   - Suggested: catch errors, append to feedback_log.jsonl with `gemini_response: null, error: ...`, skip the replacement email, send Luke a plain notice "couldn't process your feedback — try again or open Cowork."
   - Don't retry automatically (rate limit headaches).

6. **Domain verification for Resend?**
   - Current `onboarding@resend.dev` is shared and may end up in spam over time. Future work: verify a domain Luke owns and switch the From address. Not blocking; current setup works.

---

## 12. Setup checklist (one-time, for Luke)

Order matters.

- [ ] Create a private GitHub repo and push `fitness-emails/` contents (per existing README)
- [ ] Add `RESEND_API_KEY` secret (already have the key in `Fitness App/.secrets/resend_api_key.txt`)
- [ ] Trigger `daily-email.yml` workflow manually; confirm a test email arrives at `levans092@gmail.com`
- [ ] Disable the Cowork scheduled tasks (`fitness-daily-email` and `fitness-sunday-reminder`) to avoid duplicate sends
- [ ] Create the dedicated Gmail account; enable 2FA; generate App Password
- [ ] Create Gemini API key at aistudio.google.com
- [ ] Add three new GitHub secrets: `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `GEMINI_API_KEY`
- [ ] Ask Claude Code to implement Section 7 (new files + modifications + new workflow)
- [ ] Push updated repo
- [ ] Trigger `daily-email.yml` manually; confirm the 19:00 email arrives with Reply-To set correctly
- [ ] Reply to that email with test feedback (e.g., "swap this to a run")
- [ ] Trigger `process-replies.yml` manually; verify the replacement email arrives within ~30s
- [ ] Confirm `overrides.json` was committed
- [ ] Let the cron take over

---

## 13. File and path reference

Local workspace (Luke's Mac, mounted in Cowork as `/sessions/.../mnt/Fitness App/`):

```
Fitness App/
├── Phase 0 - Fitness Baseline Analysis.docx
├── Phase 1 - Pre-Baby Maintenance Plan.docx
├── Phase 2 - Postpartum Recovery Menu.docx
├── Full Body Workout.xlsx
├── hybrid-athlete-strength-program.md
├── strava activities.csv
├── strong_workouts.csv
├── .secrets/
│   └── resend_api_key.txt        # gitignored; mirrored as GitHub secret RESEND_API_KEY
└── fitness-emails/                # THIS BECOMES THE GITHUB REPO ROOT
    ├── plan_template.json
    ├── state.json
    ├── send_daily.py
    ├── send_sunday.py
    ├── .github/workflows/
    │   ├── daily-email.yml
    │   └── sunday-reminder.yml
    ├── .gitignore
    ├── README.md
    └── HANDOVER.md                # THIS FILE
    # TO BE ADDED:
    ├── gemini_client.py
    ├── process_replies.py
    ├── training_summary.py
    ├── overrides.json
    ├── feedback_log.jsonl
    ├── data/                       # weekly Strava/Strong exports for Gemini context
    │   ├── strava.csv
    │   └── strong.csv
    └── .github/workflows/
        └── process-replies.yml
```

---

## 14. Quick reference — Luke's plan structure

For the Gemini prompt and for sanity-checking proposed adjustments:

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

**Hard rules:**
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

## 15. Tone for emails

Luke prefers direct, concise, no motivational fluff. Match the tone in the existing Phase 0/1/2 docx files. The Gemini system prompt should include a tone instruction:

> Output tone: direct and concise. No "you've got this!" / "let's crush it!" /
> motivational language. Treat Luke as a competent adult who has been training
> for 8 years. Explain *why* in one sentence. Move on.

---

End of handover. Questions Claude Code should escalate back to Luke before deciding:
- Any of the items in Section 11
- Anything in this doc that conflicts with what's actually in the repo (the repo is ground truth; this doc may have drifted)
