#!/usr/bin/env python3
"""
Daily fitness email sender.

Runs from a GitHub Actions cron (or locally / from any cron-capable host).
Reads plan_template.json + state.json, builds today's email, POSTs to Resend.

Env vars required:
  RESEND_API_KEY    Resend API key (set as a GitHub repo secret)
  TO_EMAIL          Recipient (default: levans092@gmail.com)
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

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

API_KEY = os.environ.get("RESEND_API_KEY")
TO_EMAIL = os.environ.get("TO_EMAIL") or "levans092@gmail.com"
FROM_EMAIL = os.environ.get("FROM_EMAIL") or "Luke's Fitness Bot <onboarding@resend.dev>"
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
RESEND_URL = "https://api.resend.com/emails"


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def check_local_time_window():
    """We may run from multiple UTC cron entries (to cover DST). Only send if
    Amsterdam local time is within 30 min of 19:00. Manual workflow_dispatch
    runs bypass the gate so testing is easy."""
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("[note] Manual workflow_dispatch — bypassing time gate for test send.")
        return
    now = datetime.now(TZ_AMSTERDAM)
    target_minutes = 19 * 60
    now_minutes = now.hour * 60 + now.minute
    diff = abs(now_minutes - target_minutes)
    if diff > 30:
        print(f"[skip] Amsterdam local time {now.strftime('%H:%M')} is outside 19:00 ±30min window.")
        sys.exit(0)


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
  <div style="font-size: 15px; font-weight: 600; color: #1F3A5F; margin-bottom: 8px;">Today's food log</div>
  <p style="font-size: 13px; color: #555; margin: 0 0 12px 0;">Reply with what you ate today — your nutrition coach will flag anything worth adjusting around training.</p>
  <table style="font-size: 13px; color: #444; border-collapse: collapse; width: 100%;">
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Breakfast</td><td style="padding: 5px 0; border-bottom: 1px solid #EEE; width: 100%;"></td></tr>
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Lunch</td><td style="padding: 5px 0; border-bottom: 1px solid #EEE;"></td></tr>
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Dinner</td><td style="padding: 5px 0; border-bottom: 1px solid #EEE;"></td></tr>
    <tr><td style="padding: 5px 14px 5px 0; color: #888; white-space: nowrap;">Snacks</td><td style="padding: 5px 0;"></td></tr>
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


def build_phase1_html(day: dict, tomorrow: dict, day_num: int, today: date, hard_rules: list) -> str:
    date_str = today.strftime("%a %d %b %Y")
    header = html_header(
        f"Phase 1 · Day {day_num}",
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
    body += '<p style="color: #888; font-size: 13px; margin-top: 24px;">Where you are: Phase 1, pre-baby maintenance. Sub-3:25 marathon target for 22 Nov 2026.</p>'
    body += html_food_reminder()

    return f"<!DOCTYPE html><html><body style=\"{CSS_BASE}\">{header}{body}</body></html>"


def build_phase1_text(day: dict, tomorrow: dict, day_num: int, today: date, hard_rules: list) -> str:
    date_str = today.strftime("%a %d %b %Y")
    lines = []
    lines.append(f"PHASE 1 · DAY {day_num} — {date_str}")
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
    lines.append("Where you are: Phase 1, pre-baby maintenance. Sub-3:25 marathon target 22 Nov 2026.")
    lines.append("")
    lines.append("─" * 70)
    lines.append("TODAY'S FOOD LOG")
    lines.append("Reply with what you ate — your nutrition coach will flag anything worth adjusting.")
    lines.append("")
    lines.append("  Breakfast:")
    lines.append("  Lunch:")
    lines.append("  Dinner:")
    lines.append("  Snacks:")

    return "\n".join(lines)


def build_phase2_html(menu: dict, today: date, baby_birth_date_str: str | None) -> str:
    week_str = today.strftime("%d %b %Y")
    if baby_birth_date_str:
        bbd = date.fromisoformat(baby_birth_date_str)
        days_post = (today - bbd).days
        weeks_post = days_post // 7
        header_sub = f"Week {weeks_post + 1} postpartum"
    else:
        header_sub = "Postpartum recovery"

    header = html_header("Phase 2 · Weekly digest", f"Week of {week_str}", header_sub)

    grid_rows = []
    for row in menu["grid"]:
        grid_rows.append(f"""
    <tr>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee; font-weight: 600; background: #F8F9FB;">{row['time']}</td>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{row['wrecked']}</td>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{row['tired']}</td>
      <td style="padding: 8px 10px; border-bottom: 1px solid #eee;">{row['decent']}</td>
    </tr>""")

    grid_html = f"""
<table style="border-collapse: collapse; width: 100%; font-size: 13px;">
  <thead>
    <tr style="background: #1F3A5F; color: white;">
      <th style="padding: 8px 10px; text-align: left;">Time</th>
      <th style="padding: 8px 10px; text-align: left;">Wrecked (sleep &lt;4h)</th>
      <th style="padding: 8px 10px; text-align: left;">Tired (4-6h)</th>
      <th style="padding: 8px 10px; text-align: left;">Decent (6h+)</th>
    </tr>
  </thead>
  <tbody>{''.join(grid_rows)}
  </tbody>
</table>"""

    checklist = "\n".join(f"    <li>{m}</li>" for m in menu["readiness_for_phase3"])
    body = f"""
<p>{menu['preamble']}</p>
<h3 style="color: #1F3A5F; border-bottom: 1px solid #eee; padding-bottom: 4px;">The menu</h3>
{grid_html}
<h3 style="color: #1F3A5F; border-bottom: 1px solid #eee; padding-bottom: 4px;">Ready for Phase 3?</h3>
<ul style="color: #444;">
{checklist}
</ul>
<p style="color: #888; font-size: 13px; margin-top: 24px;">When all four are true, reply to this email or ping Cowork with "I'm ready" and I'll write the marathon build.</p>
"""
    return f"<!DOCTYPE html><html><body style=\"{CSS_BASE}\">{header}{body}</body></html>"


def build_phase2_text(menu: dict, today: date, baby_birth_date_str: str | None) -> str:
    week_str = today.strftime("%d %b %Y")
    lines = [f"PHASE 2 · WEEKLY DIGEST — Week of {week_str}", "=" * 70, ""]
    if baby_birth_date_str:
        bbd = date.fromisoformat(baby_birth_date_str)
        days_post = (today - bbd).days
        lines.append(f"Week {days_post // 7 + 1} postpartum.")
        lines.append("")
    lines.append(menu["preamble"])
    lines.append("")
    lines.append("THE MENU:")
    for row in menu["grid"]:
        lines.append(f"  {row['time']}")
        lines.append(f"    Wrecked: {row['wrecked']}")
        lines.append(f"    Tired:   {row['tired']}")
        lines.append(f"    Decent:  {row['decent']}")
        lines.append("")
    lines.append("READY FOR PHASE 3?")
    for m in menu["readiness_for_phase3"]:
        lines.append(f"  - {m}")
    lines.append("")
    lines.append("When all four are true, reply or ping Cowork with 'I'm ready' to trigger Phase 3.")
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

    check_local_time_window()

    plan = load_json(ROOT / "plan_template.json")
    state = load_json(ROOT / "state.json")
    phase = state["current_phase"]

    today_local = datetime.now(TZ_AMSTERDAM).date()
    date_str = today_local.strftime("%a %d %b")  # used by phase2/3

    if phase == "phase1":
        # Email is sent at 19:00 as a preview of tomorrow's session.
        target_date = today_local + timedelta(days=1)
        start = date.fromisoformat(plan["phase1_start_date"])
        days_in = (target_date - start).days
        if days_in < 0:
            sys.exit(f"Target date {target_date} is before phase1 start {start}; nothing to send.")
        cycle = plan["phase1_cycle_length_days"]
        day_num = (days_in % cycle) + 1
        day_after_num = ((days_in + 1) % cycle) + 1
        day = next(d for d in plan["phase1_days"] if d["day_num"] == day_num)
        day_after = next(d for d in plan["phase1_days"] if d["day_num"] == day_after_num)

        # Check for a feedback override for target_date
        overrides_data = load_json(ROOT / "overrides.json")
        override = overrides_data.get("overrides", {}).get(target_date.isoformat())
        if override:
            day = override["session"]
            print(f"[override] Using feedback override for {target_date.isoformat()}")

        date_str = target_date.strftime("%a %d %b")
        subject = f"Fitness plan — {date_str} (Day {day_num}) — {day['session_type']}"
        html = build_phase1_html(day, day_after, day_num, target_date, plan["hard_rules_phase1"])
        text = build_phase1_text(day, day_after, day_num, target_date, plan["hard_rules_phase1"])

    elif phase == "phase2":
        # Send only on Mondays (weekday == 0)
        if today_local.weekday() != 0:
            print(f"[skip] Phase 2: today is {today_local.strftime('%A')}, only sending Mondays.")
            return
        subject = f"Fitness plan — week of {date_str} — Phase 2 menu"
        html = build_phase2_html(plan["phase2_menu"], today_local, state.get("baby_birth_date"))
        text = build_phase2_text(plan["phase2_menu"], today_local, state.get("baby_birth_date"))

    elif phase == "phase3":
        weeks = plan.get("phase3", {}).get("weeks", [])
        if not weeks:
            subject = f"Fitness plan — {date_str} — Phase 3 not yet built"
            html = f"""<!DOCTYPE html><html><body style="{CSS_BASE}">
{html_header("Phase 3", date_str, "Plan not yet built")}
<p>Phase 3 marathon build hasn't been written yet. Open Cowork and ask Claude to build it.</p>
<p>Today: easy 30-min run if you want, otherwise rest.</p>
</body></html>"""
            text = f"Phase 3 plan not yet built. Open Cowork and ask Claude to build it. Today: easy 30-min run or rest."
        else:
            # Look up today's session by date in phase3.weeks
            # Each week entry expected to have: {week_num, start_date, sessions: [{date, ...}]}
            today_iso = today_local.isoformat()
            found = None
            for week in weeks:
                for session in week.get("sessions", []):
                    if session.get("date") == today_iso:
                        found = (week, session)
                        break
                if found:
                    break
            if not found:
                subject = f"Fitness plan — {date_str} — Phase 3 (no session today)"
                html = f"<!DOCTYPE html><html><body style=\"{CSS_BASE}\">{html_header('Phase 3', date_str, 'No session scheduled')}<p>Rest day or unscheduled. Check the plan in your folder.</p></body></html>"
                text = f"No Phase 3 session scheduled for {date_str}. Rest day or check plan."
            else:
                # Phase 3 session rendering — placeholder, will be expanded when Phase 3 is written
                week, session = found
                week_num = week.get("week_num")
                session_type = session.get("session_type", "")
                session_json = json.dumps(session, indent=2)
                subject = f"Fitness plan — {date_str} — Phase 3 Week {week_num}: {session_type}"
                header = html_header(f"Phase 3 · Week {week_num}", date_str, session_type)
                html = f"<!DOCTYPE html><html><body style=\"{CSS_BASE}\">{header}<pre>{session_json}</pre></body></html>"
                text = f"Phase 3 session for {date_str}:\n\n{session_json}"

    elif phase == "paused":
        print(f"[skip] current_phase is 'paused'; no email sent.")
        return
    else:
        sys.exit(f"Unknown phase: {phase}")

    ok = send_via_resend(subject, html, text)
    if not ok:
        sys.exit("Resend send failed (non-200 response).")


if __name__ == "__main__":
    main()
