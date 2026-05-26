#!/usr/bin/env python3
"""
Sunday 08:00 weekly summary.

Generates an AI-written week review with three options for the coming week and a
recommendation. Sends at 08:00 Amsterdam time every Sunday. Skips if mode is
survival or paused.

Env vars required:
  RESEND_API_KEY    Resend API key
  GEMINI_API_KEY    Gemini API key
  TO_EMAIL          Recipient (default: levans092@gmail.com)
  FROM_EMAIL        Sender (default: Luke's Fitness Bot <onboarding@resend.dev>)
  GMAIL_USER        Bot Gmail address (for Reply-To header)
"""

import html as html_lib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import coach_orchestrator
import training_summary as ts

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY") or ""
TO_EMAIL = os.environ.get("TO_EMAIL") or "levans092@gmail.com"
FROM_EMAIL = os.environ.get("FROM_EMAIL") or "Luke's Fitness Bot <onboarding@resend.dev>"
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
RESEND_URL = "https://api.resend.com/emails"

CSS_BASE = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; "
    "color: #222; max-width: 640px; line-height: 1.5;"
)


# ──────────────────────────────────────────────────────────────────────────
# Time gate
# ──────────────────────────────────────────────────────────────────────────

def check_local_time_window():
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("[note] Manual workflow_dispatch — bypassing time gate.")
        return
    now = datetime.now(TZ_AMSTERDAM)
    if now.weekday() != 6:
        print(f"[skip] Today is {now.strftime('%A')}, not Sunday.")
        sys.exit(0)
    target = 8 * 60
    cur = now.hour * 60 + now.minute
    if abs(cur - target) > 30:
        print(f"[skip] Amsterdam time {now.strftime('%H:%M')} is outside 08:00 ±30min.")
        sys.exit(0)


# ──────────────────────────────────────────────────────────────────────────
# Plan helpers
# ──────────────────────────────────────────────────────────────────────────

def _compute_standard_week(today: date, plan: dict) -> str:
    """Return a compact day-by-day summary of the coming week (Mon–Sun after today)."""
    start = date.fromisoformat(plan["phase1_start_date"])
    cycle = plan["phase1_cycle_length_days"]
    lines = []
    for offset in range(1, 8):
        day_date = today + timedelta(days=offset)
        days_in = (day_date - start).days
        day_num = (days_in % cycle) + 1
        session = next(d for d in plan["phase1_days"] if d["day_num"] == day_num)
        label = day_date.strftime("%a %d %b")
        lines.append(f"{label}: {session['session_type']} ({session['duration_min']} min)")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Email builders
# ──────────────────────────────────────────────────────────────────────────

def _option_html(letter: str, opt: dict, is_rec: bool) -> str:
    border = "#1F3A5F" if is_rec else "#DDD"
    bg = "#EEF3FB" if is_rec else "#FAFAFA"
    label = html_lib.escape(opt["label"])
    sessions = html_lib.escape(opt["sessions"]).replace("\n", "<br>")
    rationale = html_lib.escape(opt["rationale"])
    badge = (
        ' &nbsp;<span style="background:#1F3A5F; color:white; font-size:11px; '
        'padding:2px 7px; border-radius:3px; vertical-align:middle; letter-spacing:0.3px;">'
        'Recommended</span>'
        if is_rec else ""
    )
    return (
        f'<div style="border:1px solid {border}; background:{bg}; border-radius:4px; '
        f'padding:14px 16px; margin-bottom:12px;">'
        f'<div style="font-weight:600; font-size:15px; margin-bottom:8px;">'
        f'{html_lib.escape(letter)} — {label}{badge}</div>'
        f'<div style="font-size:13px; color:#444; font-family:monospace; '
        f'white-space:pre-wrap; margin-bottom:8px;">{sessions}</div>'
        f'<div style="font-size:13px; color:#666; font-style:italic;">{rationale}</div>'
        f'</div>'
    )


def build_html(summary: dict, week_label: str) -> str:
    rec = summary["recommendation"]
    coach_note_html = ""
    if summary.get("coach_note"):
        coach_note_html = (
            '<div style="background:#FFF8E7; border-left:3px solid #F0A500; '
            'padding:10px 14px; border-radius:2px; margin-bottom:20px; font-size:14px;">'
            f'<strong>Coach note:</strong> {html_lib.escape(summary["coach_note"])}'
            '</div>'
        )

    options_html = "".join(
        _option_html(letter, summary[key], rec == letter)
        for letter, key in [("A", "option_a"), ("B", "option_b"), ("C", "option_c")]
    )

    return (
        f'<!DOCTYPE html><html><body style="{CSS_BASE}">'
        f'<div style="border-left:4px solid #1F3A5F; padding-left:14px; margin-bottom:20px;">'
        f'<div style="color:#555; font-size:13px; text-transform:uppercase; letter-spacing:0.5px;">Sunday · 08:00</div>'
        f'<div style="font-size:22px; font-weight:600; color:#1F3A5F; margin-top:4px;">'
        f'Week ahead — {html_lib.escape(week_label)}</div>'
        f'</div>'
        f'<div style="background:#F5F5F5; border-radius:4px; padding:12px 14px; '
        f'margin-bottom:20px; font-size:14px;">'
        f'<strong>Last week:</strong> {html_lib.escape(summary["week_review"])}'
        f'</div>'
        f'{coach_note_html}'
        f'{options_html}'
        f'<div style="border-top:1px solid #EEE; margin:20px 0 16px;"></div>'
        f'<p style="font-size:14px; margin:0 0 8px;">'
        f'<strong>Recommendation: {html_lib.escape(rec)}</strong> — '
        f'{html_lib.escape(summary["recommendation_reason"])}</p>'
        f'<p style="font-size:13px; color:#888; margin:0;">'
        f'Reply with A, B, C, or describe your own preference. '
        f'Your reply adjusts tomorrow\'s session.</p>'
        f'</body></html>'
    )


def build_text(summary: dict, week_label: str) -> str:
    rec = summary["recommendation"]
    lines = [
        f"Week ahead — {week_label}",
        f"Last week: {summary['week_review']}",
        "",
    ]
    if summary.get("coach_note"):
        lines += [f"Coach note: {summary['coach_note']}", ""]
    for letter, key in [("A", "option_a"), ("B", "option_b"), ("C", "option_c")]:
        opt = summary[key]
        marker = " ← Recommended" if rec == letter else ""
        lines += [
            f"Option {letter}{marker}: {opt['label']}",
            opt["sessions"],
            opt["rationale"],
            "",
        ]
    lines += [
        f"Recommendation: {rec} — {summary['recommendation_reason']}",
        "",
        "Reply with A, B, C, or describe your own preference.",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Send
# ──────────────────────────────────────────────────────────────────────────

def send_email(subject: str, html: str, text: str) -> bool:
    payload: dict = {
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
            "-H", f"Authorization: Bearer {RESEND_API_KEY}",
            "-H", "Content-Type: application/json",
            "--data-binary", json.dumps(payload),
        ],
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    return "HTTP_STATUS:200" in result.stdout


# ──────────────────────────────────────────────────────────────────────────
# State helpers
# ──────────────────────────────────────────────────────────────────────────

def _update_weekly_counters(stats: dict, week_start: date) -> None:
    """Reset adaptation_state.md weekly counters and advance week_start to coming Monday."""
    path = ROOT / "adaptation_state.md"
    if not path.exists():
        return
    content = path.read_text()
    content = re.sub(r"(?m)^week_start: \S+", f"week_start: {week_start.isoformat()}", content)
    content = re.sub(r"(?m)^strength_sessions: \S+", f"strength_sessions: {stats['strength_sessions']}", content)
    content = re.sub(r"(?m)^run_sessions: \S+", f"run_sessions: {stats['run_sessions']}", content)
    content = re.sub(r"(?m)^run_km_total: \S+", f"run_km_total: {stats['run_km_total']}", content)
    path.write_text(content)
    print(f"[state] Weekly counters updated: {stats}")


def _write_current_week_plan(standard_week: str, week_label: str, week_start: date) -> None:
    """Overwrite plans/current-week.md with the coming week's sessions."""
    (ROOT / "plans").mkdir(exist_ok=True)
    path = ROOT / "plans" / "current-week.md"
    content = (
        f"---\nweek_label: {week_label}\nweek_start: {week_start.isoformat()}\n---\n\n"
        f"# Week ahead — {week_label}\n\n{standard_week}\n"
    )
    path.write_text(content)
    print(f"[plan] Written plans/current-week.md for {week_label}.")


def _save_pending_choice(summary: dict, week_label: str, week_start: date) -> None:
    """Write plans/pending-choice.json so process_replies.py can handle A/B/C replies."""
    (ROOT / "plans").mkdir(exist_ok=True)
    expires = (week_start + timedelta(days=7)).isoformat()
    payload = {
        "week_label": week_label,
        "week_start": week_start.isoformat(),
        "expires": expires,
        "options": {
            "A": summary["option_a"],
            "B": summary["option_b"],
            "C": summary["option_c"],
        },
        "recommendation": summary["recommendation"],
        "chosen": None,
    }
    path = ROOT / "plans" / "pending-choice.json"
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    if not RESEND_API_KEY:
        sys.exit("RESEND_API_KEY env var not set.")
    if not (os.environ.get("GEMINI_API_KEY") or ""):
        sys.exit("GEMINI_API_KEY env var not set.")

    check_local_time_window()

    with open(ROOT / "state.json") as f:
        state = json.load(f)

    mode = state.get("mode", "normal")
    if mode in ("survival", "paused"):
        print(f"[skip] Mode is '{mode}' — no Sunday summary sent.")
        return

    with open(ROOT / "plan_template.json") as f:
        plan = json.load(f)

    today = datetime.now(TZ_AMSTERDAM).date()
    coach_orchestrator.sync_taper_state(today)
    week_start = today + timedelta(days=1)
    week_end = today + timedelta(days=7)
    week_label = f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"

    # Sync last week's training stats into adaptation_state.md and roll week_start forward
    stats = ts.build_stats(days=7)
    _update_weekly_counters(stats, week_start)

    standard_week = _compute_standard_week(today, plan)
    training_text = ts.build_summary(days=14)

    # Write the coming week's session list to plans/current-week.md
    _write_current_week_plan(standard_week, week_label, week_start)

    print("[gemini] Generating weekly summary...")
    try:
        summary = coach_orchestrator.generate_weekly_summary(
            training_summary=training_text,
            standard_week=standard_week,
        )
    except Exception as exc:
        sys.exit(f"Weekly summary generation failed: {exc}")

    html = build_html(summary, week_label)
    text = build_text(summary, week_label)
    subject = f"Week ahead — {week_label}"

    print(f"[send] {subject}")
    if not send_email(subject, html, text):
        sys.exit("Send failed.")
    print("[ok] Sunday summary sent.")

    # Persist A/B/C options for reply detection this week
    _save_pending_choice(summary, week_label, week_start)
    print("[ok] Pending choice saved to plans/pending-choice.json.")


if __name__ == "__main__":
    main()
