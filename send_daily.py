#!/usr/bin/env python3
"""
Daily fitness email sender.

Runs from a GitHub Actions cron (or locally / from any cron-capable host).
Reads plan_template.json + state.json, builds today's email, POSTs to Resend.

Env vars required:
  RESEND_API_KEY    Resend API key (set as a GitHub repo secret)
  TO_EMAIL          Recipient override (default: the active profile's email)
  FROM_EMAIL        Sender (default: Luke's Fitness Bot <onboarding@resend.dev>)

Cloudflare blocks Python urllib's user-agent; we shell out to curl for the POST.
"""

import json
import os
import sys
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import notify_telegram
import progression
import routine_selector
import store
from profile import default_profile

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

API_KEY = os.environ.get("RESEND_API_KEY")
TO_EMAIL = os.environ.get("TO_EMAIL") or default_profile().email
FROM_EMAIL = os.environ.get("FROM_EMAIL") or "Luke's Fitness Bot <onboarding@resend.dev>"
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
RESEND_URL = "https://api.resend.com/emails"


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# GitHub Actions cron is best-effort and routinely fires 1-2.5h late. We guard against
# duplicates with a per-day marker in the store, so the first qualifying run each day
# sends and later/extra runs skip. A late email beats none, so there is no upper cut-off:
# any run from 15:00 local until midnight will send. 20:00 is only a preference (we start
# the cron early so the send normally lands before it); the 15:00 floor stops a run that
# slips past midnight from firing the next day's preview in the small hours.
SEND_AFTER_MINUTES = 15 * 60        # 15:00 local, earliest acceptable send (hard floor)
PREFERRED_BEFORE_MINUTES = 20 * 60  # 20:00 local, preferred-by latest (soft; we still send)


def should_send_now(profile_id: str, today_local) -> bool:
    """Decide whether to send today's email.

    Already sent today -> skip (unless FORCE_SEND=1). Manual workflow_dispatch and
    FORCE_SEND bypass the time window for testing; scheduled runs send only inside
    the 18:30-23:30 local window, which tolerates GitHub's scheduler delay."""
    forced = os.environ.get("FORCE_SEND") == "1"
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    if not forced and store.get_state(profile_id).get("last_email_sent_date") == today_local.isoformat():
        print(f"[skip] Daily email already sent for {today_local.isoformat()}.")
        return False

    if forced or manual:
        if manual:
            print("[note] Manual workflow_dispatch — bypassing time window for test send.")
        return True

    now = datetime.now(TZ_AMSTERDAM)
    now_minutes = now.hour * 60 + now.minute
    if now_minutes < SEND_AFTER_MINUTES:
        print(f"[skip] Amsterdam local time {now.strftime('%H:%M')} is before the 15:00 earliest-send time.")
        return False
    if now_minutes > PREFERRED_BEFORE_MINUTES:
        print(f"[note] Amsterdam local time {now.strftime('%H:%M')} is past the preferred 20:00; sending late rather than skipping.")
    return True


# ──────────────────────────────────────────────────────────────────────────
# HTML / text rendering
# ──────────────────────────────────────────────────────────────────────────

CSS_BASE = """font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #222; max-width: 640px; line-height: 1.5;"""


def html_header(label_top: str, label_main: str, label_sub: str) -> str:
    return f"""
<div style="border-left: 4px solid #1F3A5F; padding-left: 14px; margin-bottom: 20px;">
  <div style="color: #555; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px;">{label_top}</div>
  <div style="font-size: 22px; font-weight: 600; color: #1F3A5F; margin-top: 4px;">{label_main}</div>
  <div style="color: #777; font-size: 14px;">{label_sub}</div>
</div>"""


def html_exercise_table(exercises: list) -> str:
    rows = []
    for i, ex in enumerate(exercises, 1):
        bg = "background: #F8F9FB;" if i % 2 == 0 else ""
        rows.append(f"""
    <tr style="{bg}">
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{i}</td>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{ex['name']}</td>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{ex['sets_reps']}</td>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{ex['weight']}</td>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{ex['rest']}</td>
    </tr>""")

    return f"""
<table style="border-collapse: collapse; width: 100%; font-size: 14px;">
  <thead>
    <tr style="background: #1F3A5F; color: white;">
      <th style="padding: 8px 10px; text-align: left;">#</th>
      <th style="padding: 8px 10px; text-align: left;">Exercise</th>
      <th style="padding: 8px 10px; text-align: left;">Sets &times; Reps</th>
      <th style="padding: 8px 10px; text-align: left;">Weight</th>
      <th style="padding: 8px 10px; text-align: left;">Rest</th>
    </tr>
  </thead>
  <tbody>{''.join(rows)}
  </tbody>
</table>"""


def html_callout_yellow(text: str) -> str:
    return f"""
<div style="background: #FFF8E5; border-left: 3px solid #E8A33D; padding: 10px 14px; margin: 20px 0; border-radius: 2px;">
  <strong style="color: #8B6914;">If short on time or tired:</strong> {text}
</div>"""


def html_food_reminder() -> str:
    return """
<div style="border-top: 1px solid #EEE; margin-top: 28px; padding-top: 20px;">
  <div style="font-size: 15px; font-weight: 600; color: #1F3A5F; margin-bottom: 8px;">Reply to this email</div>
  <p style="font-size: 13px; color: #555; margin: 0 0 12px 0;">Reply with what you ate today and any feedback on tomorrow's session — both can go in one reply.</p>
  <table style="font-size: 13px; color: #444; border-collapse: collapse; width: 100%;">
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Breakfast</td><td style="padding: 5px 0; border-bottom: 1px solid #EEE; width: 100%;"></td></tr>
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Lunch</td><td style="padding: 5px 0; border-bottom: 1px solid #EEE;"></td></tr>
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Dinner</td><td style="padding: 5px 0; border-bottom: 1px solid #EEE;"></td></tr>
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Snacks</td><td style="padding: 5px 0; border-bottom: 1px solid #EEE;"></td></tr>
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Session feedback</td><td style="padding: 5px 0; font-size: 12px; color: #AAA;">(optional — e.g. "swap the run for strength")</td></tr>
  </table>
</div>"""


def html_callout_green(rules: list) -> str:
    items = "\n".join(f"    <li>{r}</li>" for r in rules)
    return f"""
<div style="background: #EAF3EA; border-left: 3px solid #4F8A4F; padding: 10px 14px; margin: 20px 0; border-radius: 2px;">
  <strong style="color: #2E5A2E;">Hard rules:</strong>
  <ul style="margin: 6px 0 0 0; padding-left: 20px; color: #2E5A2E;">
{items}
  </ul>
</div>"""


def build_cycle_html(day: dict, tomorrow: dict, day_num: int, today: date, hard_rules: list,
                     progression_note: str = "") -> str:
    date_str = today.strftime("%a %d %b %Y")
    header = html_header(
        f"Day {day_num}",
        date_str,
        f"{day['session_type']} · ~{day.get('duration_min', '')} min"
    )

    if day["session_kind"] == "strength":
        body = f"""
<h3 style="color: #1F3A5F; border-bottom: 1px solid #eee; padding-bottom: 4px;">Tomorrow's session</h3>
<p style="margin: 0 0 10px 0;"><em>Warm-up: {day.get('warm_up', '')}. Then:</em></p>
{html_exercise_table(day['exercises'])}
<p style="font-size: 13px; color: #555; margin-top: 12px;">RIR 3 = leave 3 reps in the tank on top sets. No grinders.</p>
{html_callout_yellow(day['short_version'])}
"""
        if day.get("purpose"):
            body += f"""
<h3 style="color: #1F3A5F; border-bottom: 1px solid #eee; padding-bottom: 4px;">Why this session</h3>
<p>{day['purpose']}</p>
"""
    elif day["session_kind"] == "run":
        r = day["run_details"]
        body = f"""
<h3 style="color: #1F3A5F; border-bottom: 1px solid #eee; padding-bottom: 4px;">Tomorrow's session</h3>
<table style="border-collapse: collapse; font-size: 14px; margin: 8px 0;">
  <tr><td style="padding: 4px 12px 4px 0; color: #555;">Distance</td><td style="padding: 4px 0; font-weight: 500;">{r.get('distance', '')}</td></tr>
  <tr><td style="padding: 4px 12px 4px 0; color: #555;">Duration</td><td style="padding: 4px 0; font-weight: 500;">{r.get('duration', '')}</td></tr>
  <tr><td style="padding: 4px 12px 4px 0; color: #555;">Pace</td><td style="padding: 4px 0; font-weight: 500;">{r.get('pace', '')}</td></tr>
  <tr><td style="padding: 4px 12px 4px 0; color: #555;">HR target</td><td style="padding: 4px 0; font-weight: 500;">{r.get('hr_target', '')}</td></tr>
  <tr><td style="padding: 4px 12px 4px 0; color: #555;">Effort</td><td style="padding: 4px 0; font-weight: 500;">{r.get('effort', '')}</td></tr>
</table>
"""
        if day.get("extras"):
            body += f"<p style=\"color: #444;\">{day['extras']}</p>"
        body += html_callout_yellow(day['short_version'])
    else:  # rest
        body = f"""
<h3 style="color: #1F3A5F; border-bottom: 1px solid #eee; padding-bottom: 4px;">Tomorrow: {day['session_type']}</h3>
<p>{day.get('details', '')}</p>
{html_callout_yellow(day['short_version'])}
"""

    # Day-after-tomorrow preview
    day_after_date = today + timedelta(days=1)
    day_after_str = day_after_date.strftime("%a %d %b")
    day_after_num = tomorrow["day_num"]
    body += f"""
<h3 style="color: #1F3A5F; border-bottom: 1px solid #eee; padding-bottom: 4px;">Day after tomorrow</h3>
<p><strong>{day_after_str} (Day {day_after_num}):</strong> {tomorrow['session_type']}.</p>
"""

    body += html_callout_green(hard_rules)
    footer = "Training cycle, sub-3:25 marathon target for 22 Nov 2026."
    if progression_note:
        footer += " " + progression_note
    body += f'<p style="color: #888; font-size: 13px; margin-top: 24px;">{footer}</p>'
    body += html_food_reminder()

    return f"<!DOCTYPE html><html><body style=\"{CSS_BASE}\">{header}{body}</body></html>"


def build_cycle_text(day: dict, tomorrow: dict, day_num: int, today: date, hard_rules: list,
                     progression_note: str = "") -> str:
    date_str = today.strftime("%a %d %b %Y")
    lines = []
    lines.append(f"DAY {day_num} — {date_str}")
    lines.append(f"{day['session_type']} (~{day.get('duration_min', '')} min)")
    lines.append("=" * 70)
    lines.append("")

    if day["session_kind"] == "strength":
        lines.append(f"Warm-up: {day.get('warm_up', '')}. Then:")
        lines.append("")
        for i, ex in enumerate(day["exercises"], 1):
            lines.append(f"  {i}. {ex['name']:<32} {ex['sets_reps']:<18} {ex['weight']:<22} Rest {ex['rest']}")
        lines.append("")
        lines.append("RIR 3 = leave 3 reps in the tank on top sets.")
    elif day["session_kind"] == "run":
        r = day["run_details"]
        for label, key in [("Distance","distance"),("Duration","duration"),("Pace","pace"),("HR target","hr_target"),("Effort","effort")]:
            if r.get(key):
                lines.append(f"  {label}: {r[key]}")
        if day.get("extras"):
            lines.append("")
            lines.append(day["extras"])
    else:
        lines.append(day.get("details", ""))

    lines.append("")
    lines.append(f"Short version: {day['short_version']}")
    lines.append("")

    day_after_date = today + timedelta(days=1)
    day_after_str = day_after_date.strftime("%a %d %b")
    lines.append(f"DAY AFTER TOMORROW: {day_after_str} (Day {tomorrow['day_num']}) — {tomorrow['session_type']}")
    lines.append("")
    lines.append("HARD RULES:")
    for r in hard_rules:
        lines.append(f"  - {r}")
    lines.append("")
    footer = "Training cycle, sub-3:25 marathon target for 22 Nov 2026."
    if progression_note:
        footer += " " + progression_note
    lines.append(footer)
    lines.append("")
    lines.append("─" * 70)
    lines.append("REPLY TO THIS EMAIL")
    lines.append("Include what you ate today and any feedback on tomorrow's session — both can go in one reply.")
    lines.append("")
    lines.append("  Breakfast:")
    lines.append("  Lunch:")
    lines.append("  Dinner:")
    lines.append("  Snacks:")
    lines.append("  Session feedback (optional):")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def send_via_resend(subject: str, html: str, text: str) -> bool:
    payload = {
        "from": FROM_EMAIL,
        "to": [TO_EMAIL],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if GMAIL_USER:
        payload["reply_to"] = [GMAIL_USER]
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
    if result.stderr:
        print("STDERR:", result.stderr, file=sys.stderr)
    return "HTTP_STATUS:200" in result.stdout


def main():
    if not API_KEY:
        sys.exit("RESEND_API_KEY env var not set.")

    profile = default_profile()
    today_local = datetime.now(TZ_AMSTERDAM).date()

    if not should_send_now(profile.id, today_local):
        return

    plan = load_json(ROOT / "plan_template.json")
    state = store.get_state(profile.id)
    cycle_state = state.get("cycle_state", "active")

    if cycle_state == "active":
        # Email is sent at 19:00 as a preview of tomorrow's session.
        target_date = today_local + timedelta(days=1)
        start = date.fromisoformat(plan["cycle_start_date"])
        days_in = (target_date - start).days
        if days_in < 0:
            sys.exit(f"Target date {target_date} is before cycle start {start}; nothing to send.")
        cycle = plan["cycle_length_days"]
        day_num = (days_in % cycle) + 1
        day_after_num = ((days_in + 1) % cycle) + 1
        day = next(d for d in plan["cycle_days"] if d["day_num"] == day_num)
        day_after = next(d for d in plan["cycle_days"] if d["day_num"] == day_after_num)

        # Check for a feedback override for target_date
        override = store.get_overrides(profile.id).get(target_date.isoformat())
        progression_note = ""
        if override:
            day = override["session"]
            print(f"[override] Using feedback override for {target_date.isoformat()}")
        elif day["session_kind"] == "strength":
            # Pick a routine from the library that avoids the muscle groups trained in the
            # last few days. Deterministic (no Gemini). Falls back to the static template
            # day on any failure so the email always sends.
            try:
                selected = routine_selector.select_session(target_date)
            except Exception as exc:  # noqa: BLE001 — never block the send on selection
                print(f"[warn] routine selection failed, using template: {exc}", file=sys.stderr)
                selected = None
            if selected:
                day = selected
                print(f"[routine] Selected '{day['session_type']}' for {target_date.isoformat()}")
        else:
            # Scale the template long-run / quality-run days toward race day. Overrides win,
            # so progression only applies to the unmodified template session.
            day, progression_note = progression.apply_to_session(
                day, target_date, profile.race_date,
                marathon_pace=profile.marathon_pace,
                marathon_pace_hr=profile.marathon_pace_hr,
            )

        date_str = target_date.strftime("%a %d %b")
        subject = f"Fitness plan — {date_str} (Day {day_num}) — {day['session_type']}"
        html = build_cycle_html(day, day_after, day_num, target_date, plan["hard_rules"], progression_note)
        text = build_cycle_text(day, day_after, day_num, target_date, plan["hard_rules"], progression_note)

    elif cycle_state == "paused":
        print("[skip] cycle_state is 'paused'; no email sent.")
        return
    else:
        sys.exit(f"Unknown cycle_state: {cycle_state}")

    ok = send_via_resend(subject, html, text)
    if not ok:
        sys.exit("Resend send failed (non-200 response).")

    # Record the per-day marker so a delayed or duplicate run later today skips.
    state["last_email_sent_date"] = today_local.isoformat()
    store.set_state(profile.id, state)
    print(f"[sent] Daily email sent and marked for {today_local.isoformat()}.")

    # Mirror to Telegram if configured. Best-effort: the email is the source of truth and
    # the day is already marked, so a Telegram failure must not fail the run.
    notify_telegram.notify(subject, text)


if __name__ == "__main__":
    main()
