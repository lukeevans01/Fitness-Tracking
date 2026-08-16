#!/usr/bin/env python3
"""
Sunday 18:00 weekly summary.

Generates an AI-written week review with three options for the coming week and a
recommendation. Sends at 18:00 Amsterdam time every Sunday, late enough that the week
being reviewed is finished. Skips if mode is survival or paused.

Env vars required:
  RESEND_API_KEY    Resend API key
  GEMINI_API_KEY    Gemini API key
  TO_EMAIL          Recipient override (default: the active profile's email)
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
import notify_telegram
import nutrition_logger
import plan_writer
import progression
import store
import training_summary as ts
import weekly_load
from profile import default_profile

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY") or ""
TO_EMAIL = os.environ.get("TO_EMAIL") or default_profile().email
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
    target = 18 * 60
    cur = now.hour * 60 + now.minute
    if abs(cur - target) > 30:
        print(f"[skip] Amsterdam time {now.strftime('%H:%M')} is outside 18:00 ±30min.")
        sys.exit(0)


# ──────────────────────────────────────────────────────────────────────────
# Plan helpers
# ──────────────────────────────────────────────────────────────────────────

def _recent_weekly_km(today: date, weeks: int = progression.ANCHOR_WEEKS) -> list:
    """Actual running km for each of the last `weeks` complete weeks, oldest first.

    Weeks with no running contribute 0.0 rather than being skipped, so a lay-off shows up
    in the anchor instead of being averaged away.
    """
    try:
        from ingest import get_reader
        activities = get_reader("activities")(ts.STRAVA_CSV)
    except Exception as exc:
        print(f"[warn] could not read activities to recalibrate the anchor: {exc}")
        return []

    this_monday = today - timedelta(days=today.weekday())
    # On a Sunday the current week is over, so count it: the review runs in the evening
    # precisely so it can. Any other day it is still in progress and including it would
    # understate the week, so fall back to the last fully finished weeks.
    offset = 0 if today.weekday() == 6 else 1
    totals = []
    for index in range(weeks - 1 + offset, offset - 1, -1):
        start = this_monday - timedelta(days=7 * index)
        end = start + timedelta(days=7)
        totals.append(round(sum(
            a.distance_km for a in activities
            if a.kind == "run" and start <= a.date < end
        ), 1))
    return totals


def _auto_apply_recommended(summary: dict, week_start: date, today: date, profile) -> str:
    """Write the recommended option's week into the plan, and say what happened.

    The review is unattended, so the recommendation becomes the default rather than waiting
    for a reply that may never come. An A/B/C reply later in the week still overwrites this,
    so nothing is lost by applying early. Guardrail rejections leave the template in place.
    """
    letter = (summary.get("recommendation") or "").upper()
    option = summary.get(f"option_{letter.lower()}") if letter in ("A", "B", "C") else None
    if not option:
        return "No valid recommendation to apply, so the standard cycle stands."

    written, _, message = plan_writer.apply_week(
        profile.id,
        option.get("plan"),
        week_start,
        today=today,
        source="weekly_review_auto",
        last_week_km=plan_writer.last_week_running_km(week_start, ts.STRAVA_CSV),
        race_date=profile.race_date,
    )
    print(f"[auto-apply] option {letter}: {message}")

    if not written:
        return f"Option {letter} was not applied. {message}"
    return (
        f"Option {letter} ({option.get('label', '')}) has been applied to your plan "
        f"automatically. {message} Reply A, B or C at any point this week to switch."
    )


def _compute_standard_week(today: date, plan: dict, race_date: date | None = None,
                          volume_plan=None) -> str:
    """Return a compact day-by-day summary of the coming week (Mon–Sun after today).

    When race_date is supplied, long-run and quality-run days reflect the progressed
    distances/quality for the build toward race day (deferring to the taper inside four
    weeks). Without it, the raw template is shown.
    """
    start = date.fromisoformat(plan["cycle_start_date"])
    cycle = plan["cycle_length_days"]
    lines = []
    for offset in range(1, 8):
        day_date = today + timedelta(days=offset)
        days_in = (day_date - start).days
        day_num = (days_in % cycle) + 1
        session = next(d for d in plan["cycle_days"] if d["day_num"] == day_num)
        if race_date is not None:
            session, _ = progression.apply_to_session(
                session, day_date, race_date, plan=volume_plan,
                cycle_days=plan["cycle_days"])
        label = day_date.strftime("%a %d %b")
        extra = ""
        if race_date is not None and progression.is_long_run_session(session):
            dist = (session.get("run_details") or {}).get("distance", "")
            if dist:
                extra = f", long run {dist}"
        lines.append(f"{label}: {session['session_type']} ({session['duration_min']} min){extra}")
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


def _fmt_pace(pace_min_km: float) -> str:
    mins = int(pace_min_km)
    secs = round((pace_min_km - mins) * 60)
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d}/km"


def html_weekly_exercise(breakdown: dict, week_dates: list) -> str:
    runs = breakdown.get("runs_by_date", {})
    strength = breakdown.get("strength_by_date", {})
    rows = []
    for d in week_dates:
        label = d.strftime("%a %d %b")
        run = runs.get(d)
        lifts = strength.get(d)
        parts = []
        if run:
            pace_str = f" @ {_fmt_pace(run['pace_min_km'])}" if run["pace_min_km"] else ""
            hr_str = f", HR {run['hr']:.0f}" if run["hr"] else ""
            parts.append(f"Run — {run['distance_km']} km{pace_str}{hr_str}")
        if lifts:
            lift_strs = [
                f"{ex} {w:.1f}kg×{r}" for ex, w, r in lifts
            ]
            parts.append("Strength — " + ", ".join(lift_strs))
        if parts:
            cell = "<br>".join(html_lib.escape(p) for p in parts)
        else:
            cell = '<span style="color:#AAA;">Rest / no data</span>'
        rows.append(
            f'<tr>'
            f'<td style="padding:5px 12px 5px 0; font-weight:500; white-space:nowrap;'
            f' vertical-align:top; color:#555; font-size:13px;">{label}</td>'
            f'<td style="padding:5px 0; font-size:13px; vertical-align:top;'
            f' border-bottom:1px solid #F5F5F5;">{cell}</td>'
            f'</tr>'
        )
    return (
        '<h3 style="color:#1F3A5F; border-bottom:1px solid #eee; padding-bottom:4px;'
        ' margin-top:28px;">Exercise this week</h3>'
        '<table style="border-collapse:collapse; width:100%;">'
        + "".join(rows)
        + "</table>"
    )


def text_weekly_exercise(breakdown: dict, week_dates: list) -> str:
    runs = breakdown.get("runs_by_date", {})
    strength = breakdown.get("strength_by_date", {})
    lines = ["Exercise this week:"]
    for d in week_dates:
        label = d.strftime("%a %d %b")
        run = runs.get(d)
        lifts = strength.get(d)
        parts = []
        if run:
            pace_str = f" @ {_fmt_pace(run['pace_min_km'])}" if run["pace_min_km"] else ""
            hr_str = f", HR {run['hr']:.0f}" if run["hr"] else ""
            parts.append(f"Run {run['distance_km']} km{pace_str}{hr_str}")
        if lifts:
            lift_strs = [f"{ex} {w:.1f}kg×{r}" for ex, w, r in lifts]
            parts.append("Strength: " + ", ".join(lift_strs))
        lines.append(f"  {label}: " + (" | ".join(parts) if parts else "Rest / no data"))
    return "\n".join(lines)


_MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack", "unspecified"]


def html_weekly_nutrition_detail(week_dates: list, targets: dict, profile_id: str | None = None) -> str:
    rows = []
    sign = lambda x, p=0: (f"+{x:.{p}f}" if x >= 0 else f"{x:.{p}f}")
    for d in week_dates:
        label = d.strftime("%a %d %b")
        day_log = nutrition_logger.read_day(d, profile_id)
        if not day_log or not day_log.items:
            rows.append(
                f'<tr>'
                f'<td style="padding:5px 12px 5px 0; font-weight:500; white-space:nowrap;'
                f' vertical-align:top; color:#555; font-size:13px;">{label}</td>'
                f'<td style="padding:5px 0; font-size:13px; color:#AAA;'
                f' border-bottom:1px solid #F5F5F5;">No food logged</td>'
                f'</tr>'
            )
            continue
        by_meal: dict = {}
        for item in day_log.items:
            by_meal.setdefault(item.meal, []).append(item)
        meal_lines = []
        for meal in _MEAL_ORDER:
            items = by_meal.get(meal, [])
            if not items:
                continue
            item_str = ", ".join(
                html_lib.escape(f"{i.name} ({i.quantity})") for i in items
            )
            meal_lines.append(
                f'<span style="color:#888;">{meal.capitalize()}:</span> {item_str}'
            )
        totals = nutrition_logger.daily_totals(day_log)
        dk = totals["kcal"] - targets["kcal"]
        dp = totals["protein_g"] - targets["protein_g"]
        totals_line = (
            f'<span style="font-size:12px; color:#888;">'
            f'{totals["kcal"]:.0f} kcal ({sign(dk)}), '
            f'{totals["protein_g"]:.0f}g protein ({sign(dp, 0)}g), '
            f'{totals["carbs_g"]:.0f}g carbs, '
            f'{totals["fat_g"]:.0f}g fat'
            f'</span>'
        )
        cell = "<br>".join(meal_lines) + "<br>" + totals_line
        rows.append(
            f'<tr>'
            f'<td style="padding:5px 12px 5px 0; font-weight:500; white-space:nowrap;'
            f' vertical-align:top; color:#555; font-size:13px;">{label}</td>'
            f'<td style="padding:5px 0; font-size:13px; vertical-align:top;'
            f' border-bottom:1px solid #F5F5F5;">{cell}</td>'
            f'</tr>'
        )
    return (
        '<h3 style="color:#1F3A5F; border-bottom:1px solid #eee; padding-bottom:4px;'
        ' margin-top:28px;">Nutrition this week</h3>'
        '<table style="border-collapse:collapse; width:100%;">'
        + "".join(rows)
        + "</table>"
    )


def text_weekly_nutrition_detail(week_dates: list, targets: dict, profile_id: str | None = None) -> str:
    sign = lambda x, p=0: (f"+{x:.{p}f}" if x >= 0 else f"{x:.{p}f}")
    lines = ["Nutrition this week:"]
    for d in week_dates:
        label = d.strftime("%a %d %b")
        day_log = nutrition_logger.read_day(d, profile_id)
        if not day_log or not day_log.items:
            lines.append(f"  {label}: No food logged")
            continue
        by_meal: dict = {}
        for item in day_log.items:
            by_meal.setdefault(item.meal, []).append(item)
        meal_parts = []
        for meal in _MEAL_ORDER:
            items = by_meal.get(meal, [])
            if items:
                meal_parts.append(
                    meal.capitalize() + ": " + ", ".join(f"{i.name} ({i.quantity})" for i in items)
                )
        totals = nutrition_logger.daily_totals(day_log)
        dk = totals["kcal"] - targets["kcal"]
        dp = totals["protein_g"] - targets["protein_g"]
        totals_str = (
            f"{totals['kcal']:.0f} kcal ({sign(dk)}), "
            f"{totals['protein_g']:.0f}g protein ({sign(dp, 0)}g)"
        )
        lines.append(f"  {label}: " + " | ".join(meal_parts))
        lines.append(f"    Totals: {totals_str}")
    return "\n".join(lines)


def html_weekly_aggregate(weekly_nutrition: dict, stats: dict, targets: dict) -> str:
    items = []
    run_s = stats.get("run_sessions", 0)
    run_km = stats.get("run_km_total", 0.0)
    items.append(
        f"<li><strong>Running:</strong> "
        + (f"{run_s} session(s), {run_km:.1f} km total" if run_s else "no sessions recorded")
        + "</li>"
    )
    str_s = stats.get("strength_sessions", 0)
    items.append(
        f"<li><strong>Lifting:</strong> "
        + (f"{str_s} session(s)" if str_s else "no sessions recorded")
        + "</li>"
    )
    n = weekly_nutrition["days_logged"]
    if n:
        pattern_html = ""
        if weekly_nutrition.get("patterns"):
            flags = "; ".join(html_lib.escape(p) for p in weekly_nutrition["patterns"])
            pattern_html = f' <span style="color:#8B6914;">({flags})</span>'
        items.append(
            f"<li><strong>Nutrition ({n}/7 days logged):</strong> "
            f"avg {weekly_nutrition['avg_protein_g']:.0f}g protein / {targets['protein_g']}g target, "
            f"avg {weekly_nutrition['avg_kcal']:.0f} kcal / {targets['kcal']} target, "
            f"protein target hit {weekly_nutrition['protein_target_hits']}/{n} days"
            f"{pattern_html}</li>"
        )
    else:
        items.append("<li><strong>Nutrition:</strong> no food logged this week</li>")
    return (
        '<h3 style="color:#1F3A5F; border-bottom:1px solid #eee; padding-bottom:4px;'
        ' margin-top:28px;">Week in summary</h3>'
        f'<ul style="font-size:14px; color:#444; margin:0 0 0 0; padding-left:18px;">{"".join(items)}</ul>'
    )


def text_weekly_aggregate(weekly_nutrition: dict, stats: dict, targets: dict) -> str:
    lines = ["Week in summary:"]
    run_s = stats.get("run_sessions", 0)
    run_km = stats.get("run_km_total", 0.0)
    lines.append("  Running: " + (f"{run_s} session(s), {run_km:.1f} km" if run_s else "no sessions recorded"))
    str_s = stats.get("strength_sessions", 0)
    lines.append("  Lifting: " + (f"{str_s} session(s)" if str_s else "no sessions recorded"))
    n = weekly_nutrition["days_logged"]
    if n:
        lines.append(
            f"  Nutrition ({n}/7 days): "
            f"avg {weekly_nutrition['avg_protein_g']:.0f}g protein / {targets['protein_g']}g, "
            f"avg {weekly_nutrition['avg_kcal']:.0f} kcal / {targets['kcal']}"
        )
        if weekly_nutrition.get("patterns"):
            lines.append("  Flags: " + "; ".join(weekly_nutrition["patterns"]))
    else:
        lines.append("  Nutrition: no food logged this week")
    return "\n".join(lines)


def html_improvements(improvements: dict) -> str:
    entries = [
        ("Running", improvements.get("running", "")),
        ("Lifting", improvements.get("lifting", "")),
        ("Nutrition", improvements.get("nutrition", "")),
    ]
    rows = "".join(
        f'<li><strong>{html_lib.escape(label)}:</strong> {html_lib.escape(text)}</li>'
        for label, text in entries
        if text
    )
    if not rows:
        return ""
    return (
        '<div style="background:#EAF3EA; border-left:3px solid #4F8A4F; padding:12px 14px;'
        ' margin-top:28px; border-radius:2px;">'
        '<div style="font-size:14px; font-weight:600; color:#2E5A2E; margin-bottom:8px;">'
        'Improvements for next week</div>'
        f'<ul style="font-size:13px; color:#2E5A2E; margin:0; padding-left:18px;">{rows}</ul>'
        '</div>'
    )


def text_improvements(improvements: dict) -> str:
    entries = [
        ("Running", improvements.get("running", "")),
        ("Lifting", improvements.get("lifting", "")),
        ("Nutrition", improvements.get("nutrition", "")),
    ]
    lines = ["Improvements for next week:"]
    for label, text in entries:
        if text:
            lines.append(f"  {label}: {text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def build_html(
    summary: dict,
    week_label: str,
    exercise_html: str = "",
    nutrition_html: str = "",
    aggregate_html: str = "",
    improvements_html: str = "",
) -> str:
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
        f'{exercise_html}'
        f'{nutrition_html}'
        f'{aggregate_html}'
        f'{improvements_html}'
        f'<h3 style="color:#1F3A5F; border-bottom:1px solid #eee; padding-bottom:4px; margin-top:28px;">Week ahead</h3>'
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


def build_text(
    summary: dict,
    week_label: str,
    exercise_text: str = "",
    nutrition_text: str = "",
    aggregate_text: str = "",
    improvements_text: str = "",
) -> str:
    rec = summary["recommendation"]
    lines = [
        f"Week ahead — {week_label}",
        f"Last week: {summary['week_review']}",
        "",
    ]
    if summary.get("coach_note"):
        lines += [f"Coach note: {summary['coach_note']}", ""]
    for block in (exercise_text, nutrition_text, aggregate_text, improvements_text):
        if block:
            lines += [block, ""]
    lines.append("=" * 70)
    lines.append("WEEK AHEAD")
    lines.append("=" * 70)
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

def _update_weekly_counters(
    stats: dict, week_start: date, profile_id: str, squash_sessions: int = 0
) -> None:
    """Persist last week's training counters into the store's adaptation record."""
    counters = {
        "week_start": week_start.isoformat(),
        "strength_sessions": stats["strength_sessions"],
        "run_sessions": stats["run_sessions"],
        "run_km_total": stats["run_km_total"],
        "squash_sessions": squash_sessions,
    }
    store.set_adaptation(profile_id, counters)
    print(f"[state] Weekly counters updated: {counters}")


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


def _save_pending_choice(summary: dict, week_label: str, week_start: date, profile_id: str) -> None:
    """Persist the week's A/B/C options so process_replies.py can handle A/B/C replies."""
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
    store.set_pending_choice(profile_id, payload)


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    if not RESEND_API_KEY:
        sys.exit("RESEND_API_KEY env var not set.")
    if not (os.environ.get("GEMINI_API_KEY") or ""):
        sys.exit("GEMINI_API_KEY env var not set.")

    check_local_time_window()

    profile = default_profile()
    targets = profile.daily_targets

    state = store.get_state(profile.id)

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

    # Sync last week's training stats into the store and roll week_start forward
    stats = ts.build_stats(days=7, today=today)
    load = weekly_load.build_weekly_load(days=7, today=today, profile_id=profile.id)
    _update_weekly_counters(stats, week_start, profile.id, load.squash_sessions)

    # Recalibrate the volume anchor against what was actually run, then persist it. This is
    # what stops the anchor being a constant someone has to remember to edit: every reader
    # picks the new value up from the store.
    volume_plan, anchor_note = progression.recalibrate_anchor(
        progression.VolumePlan.from_store(profile, store.get_adaptation(profile.id)),
        _recent_weekly_km(today),
        week_start - timedelta(days=week_start.weekday()),
    )
    store.set_adaptation(profile.id, volume_plan.as_store_fields())
    print(f"[anchor] {anchor_note}")

    standard_week = _compute_standard_week(today, plan, profile.race_date, volume_plan)
    training_text = ts.build_summary(days=14, today=today)
    progression_note = progression.block_label(week_start, profile.race_date, volume_plan)
    progression_note += f"\nVolume anchor: {anchor_note}"

    week_dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
    breakdown = ts.build_daily_breakdown(days=7, today=today)
    weekly_nutrition = nutrition_logger.weekly_summary(
        days=7, end_date=today, targets=targets, profile_id=profile.id
    )

    _write_current_week_plan(standard_week, week_label, week_start)

    n_logged = weekly_nutrition["days_logged"]
    if n_logged:
        nutrition_prompt_text = (
            f"{n_logged}/7 days logged. "
            f"Avg protein {weekly_nutrition['avg_protein_g']:.0f}g (target {targets['protein_g']}g). "
            f"Avg kcal {weekly_nutrition['avg_kcal']:.0f} (target {targets['kcal']}). "
            f"Protein target hit {weekly_nutrition['protein_target_hits']}/{n_logged} days."
        )
        if weekly_nutrition.get("patterns"):
            nutrition_prompt_text += " Flags: " + "; ".join(weekly_nutrition["patterns"]) + "."
    else:
        nutrition_prompt_text = "No nutrition logs this week."

    print("[gemini] Generating weekly summary...")
    try:
        summary = coach_orchestrator.generate_weekly_summary(
            training_summary=training_text,
            standard_week=standard_week,
            nutrition_summary=nutrition_prompt_text,
            profile=profile,
            weekly_load=load,
            progression_note=progression_note,
        )
    except Exception as exc:
        sys.exit(f"Weekly summary generation failed: {exc}")

    # Apply the coach's recommendation straight away. The review runs unattended, so waiting
    # for an A/B/C reply meant a week of calibration was silently discarded whenever Luke did
    # not answer. Replying still switches to another option; this only sets the default.
    applied_note = _auto_apply_recommended(summary, week_start, today, profile)
    summary["coach_note"] = (
        f"{summary.get('coach_note', '').strip()}\n\n{applied_note}".strip()
    )

    exercise_html = html_weekly_exercise(breakdown, week_dates)
    exercise_text = text_weekly_exercise(breakdown, week_dates)
    nutrition_detail_html = html_weekly_nutrition_detail(week_dates, targets, profile.id)
    nutrition_detail_text = text_weekly_nutrition_detail(week_dates, targets, profile.id)
    aggregate_html = html_weekly_aggregate(weekly_nutrition, stats, targets)
    aggregate_text_block = text_weekly_aggregate(weekly_nutrition, stats, targets)
    improv_html = html_improvements(summary.get("improvements", {}))
    improv_text = text_improvements(summary.get("improvements", {}))

    html = build_html(summary, week_label, exercise_html, nutrition_detail_html, aggregate_html, improv_html)
    text = build_text(summary, week_label, exercise_text, nutrition_detail_text, aggregate_text_block, improv_text)
    subject = f"Week ahead — {week_label}"

    print(f"[send] {subject}")
    if not send_email(subject, html, text):
        sys.exit("Send failed.")
    print("[ok] Sunday summary sent.")

    # Persist A/B/C options for reply detection this week
    _save_pending_choice(summary, week_label, week_start, profile.id)
    print("[ok] Pending choice saved to the store.")

    # Mirror to Telegram if configured. Replies are still handled over email, so the
    # A/B/C choice must be answered there; this is a notification copy only.
    notify_telegram.notify(subject, text)


if __name__ == "__main__":
    main()
