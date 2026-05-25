# Fitness emails — autonomous daily delivery + feedback loop

A GitHub Actions cron that sends Luke's daily fitness plan email via Resend at **19:00 Amsterdam time every evening** (previewing the next morning's session), without anything on his machine needing to be running. Replies are processed via Gemini AI within ~15 minutes.

## What's in here

```
fitness-emails/
├── send_daily.py              # builds and sends tomorrow's session preview email
├── send_sunday.py             # Sunday 6 PM data-refresh reminder
├── process_replies.py         # polls Gmail, calls Gemini, sends replacement emails
├── gemini_client.py           # thin Gemini 1.5 Flash REST API wrapper
├── training_summary.py        # builds compact training summary from CSVs for Gemini
├── plan_template.json         # all session data (Phase 1 rolling 10-day + Phase 2 menu + Phase 3 placeholder)
├── state.json                 # which phase Luke is currently in
├── overrides.json             # per-date session overrides from feedback replies
├── feedback_log.jsonl         # append-only log of all feedback received
├── data/
│   ├── strava.csv             # upload weekly via Sunday reminder (Strava export)
│   └── strong.csv             # upload weekly via Sunday reminder (Strong export)
├── .github/workflows/
│   ├── daily-email.yml        # cron: 17:00 + 18:00 UTC every day (19:00 Amsterdam, DST-safe)
│   ├── sunday-reminder.yml    # cron: 16:00 + 17:00 UTC every Sunday (DST-safe)
│   └── process-replies.yml    # cron: every 15 min, 17:30–04:30 UTC (19:30–05:30 Amsterdam)
└── .gitignore
```

The daily email is deterministic — given `plan_template.json` + `state.json` + tomorrow's date, it produces one email. The feedback loop adds a Gemini layer: replies to that email trigger a revised session within ~15 minutes, stored in `overrides.json` and committed back to the repo.

## One-time setup (≤ 15 min)

### 1. Create a GitHub repo

On github.com → New repository → **private** repo → name it something like `fitness-emails` → don't initialize with anything (no README, no .gitignore, nothing).

### 2. Push this folder to it

On your Mac, in Terminal:

```bash
cd "/path/to/Fitness App/fitness-emails"   # the actual path on your Mac
git init -b main
git add .
git commit -m "Initial fitness-emails setup"
git remote add origin git@github.com:YOUR_USERNAME/fitness-emails.git
git push -u origin main
```

(Use HTTPS instead of SSH if you don't have SSH keys configured — `git remote add origin https://github.com/YOUR_USERNAME/fitness-emails.git` and GitHub will prompt for a personal access token on push.)

### 3. Add secrets to the repo

#### Resend (already done)

In your new GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

- Name: `RESEND_API_KEY`
- Value: paste the key from `Fitness App/.secrets/resend_api_key.txt`

**Important:** rotate this key after the first successful run if it has ever been pasted into a chat, screenshot, or other shared surface. Generate a fresh one in Resend, update the GitHub secret, revoke the old one.

#### Feedback loop (three new secrets)

These enable reply processing. Add them the same way:

| Name | Value |
|---|---|
| `GMAIL_USER` | The bot Gmail address (e.g. `luke.fitness.bot@gmail.com`) |
| `GMAIL_APP_PASSWORD` | 16-char App Password from myaccount.google.com/apppasswords |
| `GEMINI_API_KEY` | API key from aistudio.google.com/app/apikey |

See Section 6 of HANDOVER.md for full setup instructions.

### 4. (Optional) Set up custom From/To addresses

If you want to override the defaults (`levans092@gmail.com` and `Luke's Fitness Bot <onboarding@resend.dev>`):

- Same settings page → **Variables** tab → **New repository variable**
- `TO_EMAIL` (e.g. `me@mydomain.com`)
- `FROM_EMAIL` (e.g. `Fitness Bot <fitness@mydomain.com>` — only works if you've verified that domain in Resend)

### 5. Verify it works

In the repo → **Actions** tab → **Daily fitness email** workflow → **Run workflow** (manual trigger). Wait ~30 seconds, check `levans092@gmail.com`. Email should arrive.

If it doesn't:
- Check the workflow run logs (Actions tab → click the latest run)
- Look for `HTTP_STATUS:200` in the step output
- Common failures: missing `RESEND_API_KEY` secret (401), Resend hasn't verified your recipient address (free tier requires recipient = sign-up email)

### 6. Turn off the Cowork scheduled tasks

In Cowork → Scheduled section → disable both `fitness-daily-email` and `fitness-sunday-reminder`. They're redundant now and would cause duplicate emails.

## Day-to-day operation

**Nothing. The cron runs itself.** GitHub Actions sends the evening preview at 19:00 Amsterdam time, then polls every 15 minutes for replies until 05:30 the following morning.

## Feedback loop — adjusting tomorrow's session

Reply to any daily email from `levans092@gmail.com` in natural language. The bot picks it up within 15 minutes, calls Gemini, and sends a `[Updated]` replacement email.

**Special reply commands:**

| Reply contains | What happens |
|---|---|
| Anything natural | Gemini revises the session; `[Updated]` email arrives |
| `revert` | Deletes any override, sends back the original template session |
| `switch to phase 2` / `baby born` | Transitions to Phase 2 (daily emails stop; Monday digest starts) |
| `switch to phase 3` / `I'm ready` | Transitions to Phase 3 |
| `pause` | Pauses all emails |

Multiple replies in one evening are fine — each one overwrites the previous override; Gemini sees the prior change as context.

**Training data for Gemini:** Put `strava.csv` (Strava full-history export) and `strong.csv` (Strong export) in the `data/` folder and commit them. The Sunday reminder tells you when to refresh these. Without them, Gemini still works but has no recent training context.

**Override files:**
- `overrides.json` — active per-date overrides. Delete an entry (or reply `revert`) to revert. Auto-cleaned of entries older than 7 days.
- `feedback_log.jsonl` — append-only log of all feedback received. Useful for Sunday review.

**If Gemini fails:** The bot sends you a plain notice with the error text. Reply again once the issue is resolved, or reply `revert` to stay on the template.

## When baby arrives (Phase 1 → Phase 2)

**Option A — email reply (easiest):** Reply to any fitness email with `baby born` or `switch to phase 2`. The bot will update `state.json`, commit it, and confirm by email.

**Option B — edit directly:** Edit `state.json` on your Mac:

```json
{
  "current_phase": "phase2",
  "baby_birth_date": "2026-05-27",
  "phase3_start_date": null
}
```

Then commit and push:

```bash
cd "/path/to/fitness-emails"
git add state.json
git commit -m "Baby arrived — switch to Phase 2"
git push
```

The next evening's run will see `phase2` and stop sending daily session emails. Phase 2 sends a Monday-only weekly digest instead.

## When ready for Phase 3 (marathon build)

Open Cowork, say "I'm ready for Phase 3" — I'll build the week-by-week Phase 3 plan and write it into `plan_template.json` under `phase3.weeks`. Then update `state.json`:

```json
{
  "current_phase": "phase3",
  "baby_birth_date": "2026-05-27",
  "phase3_start_date": "2026-07-06"
}
```

Commit and push. Daily emails resume with Phase 3 sessions looked up by date.

## Weekly plan adjustments

The plan is just JSON. To change next week:

1. Open `plan_template.json` (or have Cowork edit it for you after a weekly review)
2. Edit the relevant `phase1_days` entry (or `phase3.weeks` entry once Phase 3 is live)
3. Commit and push
4. Next morning's email reflects the new plan

## Testing locally

To test a script without sending:

```bash
# Set env vars
export RESEND_API_KEY="re_xxx"

# Dry-run by not setting RESEND_API_KEY (will error out before sending)
python3 send_daily.py
```

To test with a real send:

```bash
export RESEND_API_KEY="re_xxx"
python3 send_daily.py
```

Note: the script gates on Amsterdam local time being near 19:00. If you're testing at another time, comment out the `check_local_time_window()` call in `main()`.

## Cost

- Resend free tier: 3,000 emails/month. You'll use ~30.
- GitHub Actions free tier: 2,000 minutes/month for private repos. Each run takes ~30 seconds. You'll use ~10 minutes/month.

Total: $0/month, indefinitely.

## Decommissioning

If you want to stop the emails: disable both workflows in GitHub Actions (Actions tab → workflow → ⋯ → Disable workflow), or set `current_phase` to `"paused"` in `state.json` and push.
