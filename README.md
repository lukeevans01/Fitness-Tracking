# Fitness emails — autonomous daily delivery

A small GitHub Actions cron that sends Luke's daily fitness plan email via Resend at 5:30 AM Amsterdam time, every day, without anything on his machine needing to be running.

## What's in here

```
fitness-emails/
├── send_daily.py              # builds and sends today's session email
├── send_sunday.py             # Sunday 6 PM data-refresh reminder
├── plan_template.json         # all session data (Phase 1 rolling 10-day + Phase 2 menu + Phase 3 placeholder)
├── state.json                 # which phase Luke is currently in
├── .github/workflows/
│   ├── daily-email.yml        # cron: 03:30 + 04:30 UTC every day (DST-safe)
│   └── sunday-reminder.yml    # cron: 16:00 + 17:00 UTC every Sunday (DST-safe)
└── .gitignore
```

The scripts are deterministic — given `plan_template.json` + `state.json` + today's date, they produce one email. No LLM call at send time; the content lookup is just a date-modulo operation. To change the plan, edit the JSON and commit.

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

### 3. Add the Resend API key as a repo secret

In your new GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

- Name: `RESEND_API_KEY`
- Value: paste the key from `Fitness App/.secrets/resend_api_key.txt`

**Important:** rotate this key after the first successful run if it has ever been pasted into a chat, screenshot, or other shared surface. Generate a fresh one in Resend, update the GitHub secret, revoke the old one.

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

**Nothing. The cron runs itself.** GitHub Actions delivers daily at 5:30 AM Amsterdam time (with 5-15 min cron drift — accepted tradeoff for free always-on infrastructure).

## When baby arrives (Phase 1 → Phase 2)

Edit `state.json` on your Mac:

```json
{
  "current_phase": "phase2",
  "baby_birth_date": "2026-05-27",  // actual birth date
  "phase3_start_date": null
}
```

Commit and push:

```bash
cd "/path/to/fitness-emails"
git add state.json
git commit -m "Baby arrived — switch to Phase 2"
git push
```

The next morning's run will see `phase2` and send the Monday-only weekly digest instead of daily session emails.

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

Note: the script gates on Amsterdam local time being near 05:30. If you're testing at another time, comment out the `check_local_time_window()` call in `main()`.

## Cost

- Resend free tier: 3,000 emails/month. You'll use ~30.
- GitHub Actions free tier: 2,000 minutes/month for private repos. Each run takes ~30 seconds. You'll use ~10 minutes/month.

Total: $0/month, indefinitely.

## Decommissioning

If you want to stop the emails: disable both workflows in GitHub Actions (Actions tab → workflow → ⋯ → Disable workflow), or set `current_phase` to `"paused"` in `state.json` and push.
