#!/usr/bin/env python3
"""
Sunday evening data-refresh reminder.

Sends a check-in email asking Luke to upload weekly Strava/Strong data and
share feedback. Phase-aware tone (Phase 1: light; Phase 2: care-only; Phase 3: full review).

Env vars required:
  RESEND_API_KEY    Resend API key
  TO_EMAIL          Recipient (default: levans092@gmail.com)
  FROM_EMAIL        Sender (default: Luke's Fitness Bot <onboarding@resend.dev>)
"""

import json
import os
import sys
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

API_KEY = os.environ.get("RESEND_API_KEY")
TO_EMAIL = os.environ.get("TO_EMAIL") or "levans092@gmail.com"
FROM_EMAIL = os.environ.get("FROM_EMAIL") or "Luke's Fitness Bot <onboarding@resend.dev>"
RESEND_URL = "https://api.resend.com/emails"

CSS_BASE = """font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #222; max-width: 640px; line-height: 1.5;"""


def check_local_time_window():
    """Only send if Amsterdam local time is within 30 min of 18:00 on Sunday."""
    now = datetime.now(TZ_AMSTERDAM)
    if now.weekday() != 6:  # 6 = Sunday
        print(f"[skip] Today is {now.strftime('%A')}, not Sunday.")
        sys.exit(0)
    target = 18 * 60
    cur = now.hour * 60 + now.minute
    if abs(cur - target) > 30:
        print(f"[skip] Amsterdam time {now.strftime('%H:%M')} is outside 18:00 ±30min.")
        sys.exit(0)


def header_html(label_main: str, label_sub: str) -> str:
    return f"""
<div style="border-left: 4px solid #1F3A5F; padding-left: 14px; margin-bottom: 20px;">
  <div style="color: #555; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px;">Sunday check-in</div>
  <div style="font-size: 22px; font-weight: 600; color: #1F3A5F; margin-top: 4px;">{label_main}</div>
  <div style="color: #777; font-size: 14px;">{label_sub}</div>
</div>"""


def build_phase1(today: date) -> tuple[str, str, str]:
    date_str = today.strftime("%d %b %Y")
    subject = f"Sunday check-in — {date_str}: how was the week?"
    html = f"""<!DOCTYPE html><html><body style="{CSS_BASE}">
{header_html(f"Week of {date_str}", "Phase 1 · pre-baby maintenance")}
<p>Quick one. How was the past week's training?</p>
<ul>
  <li>Anything feel off — too hard, too easy, awkwardly timed?</li>
  <li>Any niggles surfacing?</li>
  <li>If you had non-zero training, drop fresh Strava + Strong exports into the Fitness App folder.</li>
</ul>
<p>Hope baby's playing fair with the due date. If she's arrived, update <code>state.json</code> (set <code>current_phase</code> to <code>"phase2"</code> and fill <code>baby_birth_date</code>) and push — the daily emails will switch to the weekly-digest mode automatically.</p>
<p style="color: #888; font-size: 13px; margin-top: 24px;">Adjustments to next week happen when you reply to this email or ping Cowork.</p>
</body></html>"""
    text = f"""Sunday check-in — {date_str}
Phase 1, pre-baby maintenance

Quick one. How was the past week's training?
- Anything feel off — too hard, too easy, awkwardly timed?
- Any niggles surfacing?
- If you had non-zero training, drop fresh Strava + Strong exports into the Fitness App folder.

If baby's arrived: update state.json (set current_phase to "phase2", fill baby_birth_date) and push.

Adjustments to next week happen when you reply or ping Cowork.
"""
    return subject, html, text


def build_phase2(today: date, state: dict) -> tuple[str, str, str]:
    date_str = today.strftime("%d %b %Y")
    bbd_str = state.get("baby_birth_date")
    weeks_post = ""
    if bbd_str:
        bbd = date.fromisoformat(bbd_str)
        weeks_post = f"Week {(today - bbd).days // 7 + 1} postpartum."
    subject = f"Sunday check-in — {date_str}: how are you doing?"
    html = f"""<!DOCTYPE html><html><body style="{CSS_BASE}">
{header_html(f"Week of {date_str}", f"Phase 2 · postpartum recovery{(' · ' + weeks_post) if weeks_post else ''}")}
<p>No agenda this week. Just: how are you?</p>
<p>If you're approaching the four readiness markers (30-40 min easy run feels comfortable, two strength sessions in a week without lingering soreness, 5+ hours sleep average, mentally ready) — open Cowork and say "I'm ready" and I'll write Phase 3.</p>
<p>If you're not there yet: no rush. The marathon is in November. Phase 3 needs ~14 weeks; you've got plenty.</p>
<p style="color: #888; font-size: 13px; margin-top: 24px;">Hope all is well.</p>
</body></html>"""
    text = f"""Sunday check-in — {date_str}
Phase 2, postpartum recovery. {weeks_post}

No agenda this week. Just: how are you?

If approaching the four readiness markers (30-40 min easy run comfortable, two strength sessions/wk without soreness, 5+h sleep avg, mentally ready) — ping Cowork with "I'm ready" and I'll write Phase 3.

If not there yet: no rush. The marathon is in November. Phase 3 needs ~14 weeks; plenty of time.
"""
    return subject, html, text


def build_phase3(today: date) -> tuple[str, str, str]:
    date_str = today.strftime("%d %b %Y")
    subject = f"Sunday check-in — {date_str}: upload this week's data"
    html = f"""<!DOCTYPE html><html><body style="{CSS_BASE}">
{header_html(f"Week of {date_str}", "Phase 3 · marathon build · weekly review")}
<p>Time to refresh the plan. Three things:</p>
<ol>
  <li><strong>Updated data.</strong> Re-export Strava (full history) and Strong, drop both in the Fitness App folder, overwrite the old files.</li>
  <li><strong>Feedback on this week.</strong> Which sessions felt right? Which felt wrong? Any niggles? Sleep quality average?</li>
  <li><strong>Open Cowork and say "review my week"</strong> — I'll re-analyse and adjust next week's plan.</li>
</ol>
<p style="color: #888; font-size: 13px; margin-top: 24px;">Aim to do this before Monday morning if you can — fresh plan ready for the week.</p>
</body></html>"""
    text = f"""Sunday check-in — {date_str}
Phase 3, marathon build weekly review

Time to refresh the plan:

1. Updated data: re-export Strava (full history) and Strong, drop both in the Fitness App folder, overwrite the old files.
2. Feedback on this week: which sessions felt right? Which wrong? Any niggles? Sleep quality average?
3. Open Cowork and say "review my week" — I'll re-analyse and adjust next week's plan.

Aim for this before Monday morning if you can.
"""
    return subject, html, text


def send_via_resend(subject: str, html: str, text: str) -> bool:
    payload = {
        "from": FROM_EMAIL,
        "to": [TO_EMAIL],
        "subject": subject,
        "html": html,
        "text": text,
    }
    result = subprocess.run(
        [
            "curl", "-s", "-w", "\nHTTP_STATUS:%{http_code}\n",
            "-X", "POST", RESEND_URL,
            "-H", f"Authorization: Bearer {API_KEY}",
            "-H", "Content-Type: application/json",
            "--data-binary", json.dumps(payload),
        ],
        capture_output=True, text=True,
    )
    print(result.stdout)
    return "HTTP_STATUS:200" in result.stdout


def main():
    if not API_KEY:
        sys.exit("RESEND_API_KEY env var not set.")

    check_local_time_window()

    with open(ROOT / "state.json") as f:
        state = json.load(f)

    today = datetime.now(TZ_AMSTERDAM).date()
    phase = state["current_phase"]

    if phase == "phase1":
        subject, html, text = build_phase1(today)
    elif phase == "phase2":
        subject, html, text = build_phase2(today, state)
    elif phase == "phase3":
        subject, html, text = build_phase3(today)
    elif phase == "paused":
        print("[skip] phase is 'paused'.")
        return
    else:
        sys.exit(f"Unknown phase: {phase}")

    if not send_via_resend(subject, html, text):
        sys.exit("Send failed.")


if __name__ == "__main__":
    main()
