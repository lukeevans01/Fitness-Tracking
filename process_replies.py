#!/usr/bin/env python3
"""
Poll Gmail inbox for replies from Luke, classify intent, dispatch to a handler.

Env vars required:
  GMAIL_USER           Bot Gmail address (e.g. luke.fitness.bot@gmail.com)
  GMAIL_APP_PASSWORD   16-char App Password (no spaces)
  GEMINI_API_KEY       Gemini API key
  RESEND_API_KEY       Resend API key
  TO_EMAIL             Recipient override (default: the active profile's email)
  FROM_EMAIL           Sender (default: Luke's Fitness Bot <onboarding@resend.dev>)
"""

import email as email_lib
import html as html_lib
import imaplib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import coach_orchestrator
import intent_classifier
import nutrition_logger
import plan_cycle
import plan_guardrails
import store
import training_summary as ts
import weekly_load
from profile import default_profile
from send_daily import (
    CSS_BASE,
    build_cycle_html,
    build_cycle_text,
)

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

GMAIL_USER = os.environ.get("GMAIL_USER") or ""
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD") or ""
RESEND_API_KEY = os.environ.get("RESEND_API_KEY") or ""
TO_EMAIL = os.environ.get("TO_EMAIL") or default_profile().email
FROM_EMAIL = os.environ.get("FROM_EMAIL") or "Luke's Fitness Bot <onboarding@resend.dev>"
RESEND_URL = "https://api.resend.com/emails"

_RE_SURVIVAL_ENTER = re.compile(
    r"\bsurvival\s+mode\b|\bpause\s+training\b|\bbaby\s+born\b|\bbaby\s+arrived\b",
    re.IGNORECASE,
)
_RE_SURVIVAL_EXIT = re.compile(
    r"\bi.?m\s+back\b|\bresume\s+training\b",
    re.IGNORECASE,
)
_RE_PAUSE_ALL = re.compile(r"^\s*pause\s*$", re.IGNORECASE)
_RE_WEEK_CHOICE = re.compile(r"^\s*([ABC])\s*[.!?]?\s*$", re.IGNORECASE)
_RE_REVERT = re.compile(r"\brevert\b", re.IGNORECASE)

def _imap_search_query(profile) -> str:
    """Build the IMAP search expression for unread replies from the profile's address."""
    return f'UNSEEN FROM "{profile.email}"'


_MODE_NOTICES = {
    "survival": (
        "Survival mode active. Daily emails paused.\n\n"
        "Reply 'I'm back' or 'resume training' when you're ready to pick up training again."
    ),
    "normal": (
        "Back in training. Daily emails will resume tonight.\n\n"
        "Training continues toward San Sebastián (22 Nov 2026), run as a controlled "
        "effort with no time goal."
    ),
    "paused": (
        "All emails paused. Edit state.json and set mode to 'normal' to resume."
    ),
}


# ──────────────────────────────────────────────────────────────────────────
# Storage (all persisted state goes through store.py, profile-keyed)
# ──────────────────────────────────────────────────────────────────────────

# The active profile for this run. Set once at the top of main(); the storage
# helpers fall back to the default profile so they remain callable in tests.
_ACTIVE_PROFILE_ID: str | None = None


def _active_profile_id() -> str:
    return _ACTIVE_PROFILE_ID or default_profile().id


def _load_json(path: Path) -> dict:
    """Read a plain JSON file (still used for the static plan_template.json)."""
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _load_overrides() -> dict:
    """Load overrides from the store into the in-memory {"overrides": {...}} shape."""
    return {"overrides": store.get_overrides(_active_profile_id())}


def _persist_override(iso_date: str, record: dict, overrides: dict) -> None:
    """Write one override to the store and mirror it into the in-memory cache."""
    store.set_override(_active_profile_id(), iso_date, record)
    overrides.setdefault("overrides", {})[iso_date] = record


def _drop_override(iso_date: str, overrides: dict) -> None:
    """Delete one override from the store and the in-memory cache."""
    store.delete_override(_active_profile_id(), iso_date)
    overrides.get("overrides", {}).pop(iso_date, None)


def _save_state(state: dict) -> None:
    store.set_state(_active_profile_id(), state)


def _append_feedback_log(entry: dict) -> None:
    store.append_feedback(_active_profile_id(), entry)


def _clean_old_overrides(overrides: dict, today: date) -> None:
    # Prune only past dates. A deliberate edit to a future day (possibly weeks out) must
    # survive until its date passes, so the cutoff is today, not "applied 7+ days ago".
    cutoff = today.isoformat()
    removed = store.clean_old_overrides(_active_profile_id(), cutoff)
    stale = [k for k in list(overrides.get("overrides", {})) if k < cutoff]
    for k in stale:
        del overrides["overrides"][k]
    if removed:
        print(f"[cleanup] Removed {removed} stale override(s).")


# ──────────────────────────────────────────────────────────────────────────
# Email parsing
# ──────────────────────────────────────────────────────────────────────────

def _strip_quoted_history(text: str) -> str:
    """Return only Luke's new text; drop standard reply quote blocks."""
    clean = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .{10,}wrote:\s*$", stripped):
            break
        if re.match(r"^-{5,}", stripped):
            break
        if re.match(r"^_{5,}", stripped):
            break
        if "Original Message" in stripped or "Forwarded message" in stripped:
            break
        clean.append(line)
    return "\n".join(clean).strip()


def _get_plain_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if (
                part.get_content_type() == "text/plain"
                and part.get("Content-Disposition") != "attachment"
            ):
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


def _decode_header(value: str) -> str:
    parts = email_lib.header.decode_header(value or "")
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


# ──────────────────────────────────────────────────────────────────────────
# Plan helpers
# ──────────────────────────────────────────────────────────────────────────

def _cycle_day(target_date: date, plan: dict) -> tuple[int, dict, dict]:
    """Return (day_num, session, day_after_session) for target_date from the training cycle.

    Thin wrapper around the shared plan_cycle.cycle_day so the dashboard builder and
    the email coach use identical date->cycle-day maths.
    """
    return plan_cycle.cycle_day(target_date, plan)


def _get_current_session(target_date: date, plan: dict, overrides: dict) -> tuple[dict, int]:
    """Return (session, day_num) for target_date, using override if present."""
    day_num, template_session, _ = _cycle_day(target_date, plan)
    target_iso = target_date.isoformat()
    if target_iso in overrides.get("overrides", {}):
        return overrides["overrides"][target_iso]["session"], day_num
    return template_session, day_num


# ──────────────────────────────────────────────────────────────────────────
# Mode management (survival / normal / paused)
# ──────────────────────────────────────────────────────────────────────────

def _detect_mode_command(text: str) -> str | None:
    """Return 'survival', 'normal', 'paused', or None."""
    if _RE_SURVIVAL_ENTER.search(text):
        return "survival"
    if _RE_SURVIVAL_EXIT.search(text):
        return "normal"
    if _RE_PAUSE_ALL.match(text):
        return "paused"
    return None


def _apply_mode_change(new_mode: str, state: dict, today_local: date) -> None:
    prev_mode = state.get("mode", "normal")
    state["mode"] = new_mode
    # Keep cycle_state in sync so send_daily.py pauses/resumes correctly
    if new_mode in ("survival", "paused"):
        state["cycle_state"] = "paused"
    elif new_mode == "normal":
        state["cycle_state"] = "active"
    _save_state(state)
    _update_adaptation_state_mode(new_mode, prev_mode, today_local)
    print(f"[mode] {prev_mode} → {new_mode}")


def _update_adaptation_state_mode(new_mode: str, prev_mode: str, today_local: date) -> None:
    """Record the mode transition in the store's adaptation record."""
    today_iso = today_local.isoformat()
    fields = {"mode": new_mode, "last_updated": today_iso}
    if new_mode == "survival" and prev_mode != "survival":
        fields["survival_started_at"] = today_iso
        fields["survival_ended_at"] = None
    elif new_mode == "normal" and prev_mode == "survival":
        fields["survival_ended_at"] = today_iso
    store.set_adaptation(_active_profile_id(), fields)


# ──────────────────────────────────────────────────────────────────────────
# Week-plan choice (A/B/C from Sunday summary)
# ──────────────────────────────────────────────────────────────────────────

def _last_week_running_km(week_start: date) -> float | None:
    """Actual running volume for the seven days before `week_start`, or None if unknown.

    Feeds the week-on-week ceiling in plan_guardrails. None means "no history", which the
    guardrail treats as "skip that particular ceiling" rather than blocking the week.
    """
    try:
        from ingest import get_reader
        activities = get_reader("activities")(ts.STRAVA_CSV)
    except Exception as exc:
        print(f"[warn] could not read activities for the load ceiling: {exc}")
        return None
    previous_start = week_start - timedelta(days=7)
    km = sum(
        a.distance_km for a in activities
        if a.kind == "run" and previous_start <= a.date < week_start
    )
    return round(km, 1) if km > 0 else None


def _write_week_plan(option: dict, week_start: date, today_local: date) -> str:
    """Validate a chosen option's structured plan and write it as per-date overrides.

    Returns a human-readable line describing what happened. The plan is optional: when the
    coach did not produce one, or it fails validation, the template stands and Luke is told
    so rather than silently getting a week he did not agree to.
    """
    plan = option.get("plan")
    if not plan:
        return "No structured plan on this option, so the standard cycle stands."

    profile = default_profile()
    verdict = plan_guardrails.validate_week(
        plan,
        week_start,
        last_week_km=_last_week_running_km(week_start),
        race_date=profile.race_date,
        today=today_local,
    )
    if not verdict.ok:
        print(f"[guardrails] rejected the proposed week: {verdict.errors}")
        return (
            "The proposed week did not pass the safety checks, so the standard cycle "
            f"stands. Reason: {'; '.join(verdict.errors)}"
        )

    applied_at = datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds")
    written = 0
    for session in verdict.days:
        iso = session["date"]
        if iso < today_local.isoformat():
            continue  # never rewrite a day that has already happened
        record = {
            "applied_at": applied_at,
            "edit_source": "weekly_review",
            "session": {k: v for k, v in session.items() if k != "date"},
        }
        store.set_override(_active_profile_id(), iso, record)
        written += 1

    detail = f"{written} of 7 days written to your plan."
    if verdict.notes:
        detail += " Adjusted for safety: " + "; ".join(verdict.notes) + "."
    return detail


def _apply_week_choice(letter: str, state: dict, today_local: date) -> str | None:
    """Record Luke's A/B/C pick and write it into the plan.

    Returns confirmation text, or None if no valid pending choice. Until this wrote
    overrides the pick was advisory only: the chosen sessions were stored as prose and
    every daily email still rendered the untouched template.
    """
    pending = store.get_pending_choice(_active_profile_id())
    if not pending:
        return None
    expires = date.fromisoformat(pending.get("expires", "2000-01-01"))
    if today_local > expires:
        return None
    letter_upper = letter.upper()
    opt = pending.get("options", {}).get(letter_upper)
    if not opt:
        return None
    state["week_choice"] = opt["sessions"]
    state["week_choice_label"] = f"Option {letter_upper} — {opt['label']}"
    _save_state(state)
    pending["chosen"] = letter_upper
    store.set_pending_choice(_active_profile_id(), pending)

    week_start = date.fromisoformat(
        pending.get("week_start") or today_local.isoformat()
    )
    outcome = _write_week_plan(opt, week_start, today_local)

    return (
        f"Week plan confirmed: Option {letter_upper} — {opt['label']}\n\n"
        f"{opt['sessions']}\n\n"
        f"{opt['rationale']}\n\n"
        f"{outcome}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Sending
# ──────────────────────────────────────────────────────────────────────────

def _send_email(subject: str, html: str, text: str) -> bool:
    payload = {
        "from": FROM_EMAIL,
        "to": [TO_EMAIL],
        "subject": subject,
        "html": html,
        "text": text,
        "reply_to": [GMAIL_USER],
    }
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
    if result.stderr:
        print("RESEND STDERR:", result.stderr, file=sys.stderr)
    return "HTTP_STATUS:200" in result.stdout


def _send_plain_notice(subject: str, body: str) -> None:
    escaped = html_lib.escape(body).replace("\n", "<br>")
    _send_email(
        subject,
        f'<!DOCTYPE html><html><body style="{CSS_BASE}"><p>{escaped}</p></body></html>',
        body,
    )


def _build_replacement_email(
    session: dict,
    coach_note: str,
    target_date: date,
    plan: dict,
) -> tuple[str, str, str]:
    """Return (subject, html, text) for a replacement email with coach_note prepended."""
    day_num, _, day_after = _cycle_day(target_date, plan)
    date_str = target_date.strftime("%a %d %b")
    hard_rules = plan["hard_rules"]

    note_html = (
        '<div style="background:#E8F0FE; border-left:3px solid #1F3A5F; '
        'padding:10px 14px; margin:0 0 20px 0; border-radius:2px;">'
        f'<strong style="color:#1F3A5F;">Coach note:</strong> '
        f"{html_lib.escape(coach_note)}</div>"
    )
    note_text = f"[Updated] Coach note: {coach_note}\n\n"

    try:
        base_html = build_cycle_html(session, day_after, day_num, target_date, hard_rules)
        base_text = build_cycle_text(session, day_after, day_num, target_date, hard_rules)
    except Exception:
        fallback_body = f"Coach note: {coach_note}\n\nSession: {json.dumps(session, indent=2)}"
        return (
            f"[Updated] Fitness plan — {date_str} — {session.get('session_type', '')}",
            f'<!DOCTYPE html><html><body style="{CSS_BASE}"><pre>{html_lib.escape(fallback_body)}</pre></body></html>',
            fallback_body,
        )

    body_tag_end = base_html.index(">", base_html.index("<body")) + 1
    full_html = base_html[:body_tag_end] + note_html + base_html[body_tag_end:]
    full_text = note_text + base_text

    subject = (
        f"[Updated] Fitness plan — {date_str} "
        f"(Day {day_num}) — {session.get('session_type', 'session')}"
    )
    return subject, full_html, full_text


# ──────────────────────────────────────────────────────────────────────────
# Intent handlers
# ──────────────────────────────────────────────────────────────────────────

def _handle_revert(
    reply_text: str,
    plan: dict,
    overrides: dict,
    tomorrow: date,
    message_id: str,
    from_addr: str,
) -> None:
    """Drop tomorrow's override if any and send the template session as a [Reverted] email."""
    tomorrow_iso = tomorrow.isoformat()
    if tomorrow_iso in overrides.get("overrides", {}):
        _drop_override(tomorrow_iso, overrides)
        original_session, day_num = _get_current_session(tomorrow, plan, overrides)
        _, _, day_after = _cycle_day(tomorrow, plan)
        date_str = tomorrow.strftime("%a %d %b")
        hard_rules = plan["hard_rules"]
        html = build_cycle_html(original_session, day_after, day_num, tomorrow, hard_rules)
        text = build_cycle_text(original_session, day_after, day_num, tomorrow, hard_rules)
        subj = (
            f"[Reverted] Fitness plan — {date_str} "
            f"(Day {day_num}) — {original_session['session_type']}"
        )
        print(f"[revert] Removed override for {tomorrow_iso}.")
    else:
        subj = f"[Revert] No override active for {tomorrow_iso}"
        html = (
            f'<!DOCTYPE html><html><body style="{CSS_BASE}">'
            f"<p>No override was active for {tomorrow_iso} — "
            "you're already on the template session.</p></body></html>"
        )
        text = f"No override active for {tomorrow_iso}."
        print(f"[revert] No override to revert for {tomorrow_iso}.")

    _send_email(subj, html, text)
    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": reply_text,
        "intent": "revert",
        "action": "revert",
        "override_applied": False,
    })


def _handle_training_feedback(
    text: str,
    plan: dict,
    overrides: dict,
    target_date: date,
    message_id: str,
    from_addr: str,
    week_context: str,
    profile,
) -> None:
    """Send the training-feedback fragment to the coach orchestrator and apply the override.

    target_date is the day being adjusted (defaults to tomorrow at the call site when no
    day is referenced in the reply). The override is keyed and the email built for it.
    """
    target_iso = target_date.isoformat()
    current_session, _ = _get_current_session(target_date, plan, overrides)
    prev_override = overrides.get("overrides", {}).get(target_iso, {}).get("session")
    today = datetime.now(TZ_AMSTERDAM).date()
    training_text = ts.build_summary(days=14, today=today)
    try:
        load = weekly_load.build_weekly_load(days=7, today=today, profile_id=profile.id)
    except Exception as exc:
        print(f"[warning] could not build weekly load: {exc}", file=sys.stderr)
        load = None

    domain = coach_orchestrator.infer_domain(current_session.get("session_kind", "strength"))
    try:
        new_session = coach_orchestrator.generate_session(
            domain=domain,
            reply_text=text,
            current_session=current_session,
            training_summary=training_text,
            previous_override=prev_override,
            week_context=week_context,
            profile=profile,
            weekly_load=load,
        )
    except (ValueError, RuntimeError) as exc:
        error_msg = str(exc)
        print(f"[error] Gemini failed: {error_msg}", file=sys.stderr)
        _append_feedback_log({
            "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
            "message_id": message_id,
            "from_address": from_addr,
            "reply_text": text,
            "intent": "training_feedback",
            "error": error_msg,
            "override_applied": False,
        })
        _send_plain_notice(
            "Fitness bot — couldn't process your feedback",
            f"Couldn't process your feedback. Error:\n\n{error_msg}\n\n"
            "Fix the issue and try again, or reply with 'revert' to stay on the template session.",
        )
        return

    _persist_override(target_iso, {
        "applied_at": datetime.now(TZ_AMSTERDAM).isoformat(),
        "feedback_source": f"reply: {text[:200]!r}",
        "session": new_session,
    }, overrides)

    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "training_feedback",
        "coach_note": new_session.get("coach_note", ""),
        "target_date": target_iso,
        "override_applied": True,
    })

    coach_note = new_session.get("coach_note", "")
    subj, html, body_text = _build_replacement_email(new_session, coach_note, target_date, plan)
    ok = _send_email(subj, html, body_text)
    if ok:
        print(f"[ok] Replacement email sent for {target_iso}.")
    else:
        print(f"[error] Replacement email send failed for {target_iso}.", file=sys.stderr)


def _is_nutrition_actionable(result) -> bool:
    """Return True when the food-log result warrants an acknowledgement email.

    Criteria (any one sufficient):
      - coach generated a note (guidance to act on)
      - protein is short by more than 30 g against today's target
      - total kcal is off by more than 500 (over or under)
    Routine, on-target logs are silently written but produce no email.
    """
    if result.coach_note:
        return True
    d = result.delta_vs_target
    if d.get("protein_g", 0) < -30:
        return True
    if abs(d.get("kcal", 0)) > 500:
        return True
    return False


def _handle_food_log(text: str, message_id: str, from_addr: str, profile) -> None:
    """Parse the food log fragment, append to nutrition_log/YYYY-MM-DD.md.

    An acknowledgement email is sent only when _is_nutrition_actionable returns True.
    The feedback log entry always records whether an email was sent.
    """
    today = datetime.now(TZ_AMSTERDAM).date()
    try:
        result = nutrition_logger.log_food(text, today, profile=profile)
    except (ValueError, RuntimeError) as exc:
        print(f"[error] Food log failed: {exc}", file=sys.stderr)
        _append_feedback_log({
            "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
            "message_id": message_id,
            "from_address": from_addr,
            "reply_text": text,
            "intent": "food_log",
            "error": str(exc),
            "override_applied": False,
        })
        _send_plain_notice(
            "Food log — couldn't parse",
            f"Couldn't parse your food log. Error:\n\n{exc}\n\n"
            "Try again with more detail (e.g. quantities), or skip and log later.",
        )
        return

    actionable = _is_nutrition_actionable(result)
    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "food_log",
        "items_logged": len(result.items),
        "running_totals": result.running_totals,
        "override_applied": False,
        "emailed": actionable,
    })

    if actionable:
        subject = f"Food logged — {today.strftime('%a %d %b')}"
        html, plain = _build_food_log_ack(result, today, profile.daily_targets)
        _send_email(subject, html, plain)
    else:
        print(f"[food_log] Log written silently — on target, no email sent.")


def _build_food_log_ack(
    result: nutrition_logger.LogResult,
    day: date,
    targets: dict,
) -> tuple[str, str]:
    """Render an acknowledgement email for a food-log reply."""
    date_str = day.strftime("%a %d %b %Y")

    logged_rows_html = "".join(
        f"<li>{html_lib.escape(i.name)} — "
        f"{html_lib.escape(i.quantity)}: "
        f"{i.kcal:.0f} kcal, {i.protein_g:.1f}g P, {i.carbs_g:.1f}g C, {i.fat_g:.1f}g F "
        f"<span style=\"color:#888;\">({html_lib.escape(i.confidence)} · {html_lib.escape(i.source)})</span></li>"
        for i in result.items
    )
    logged_rows_text = "\n".join(
        f"  - {i.name} — {i.quantity}: "
        f"{i.kcal:.0f} kcal, {i.protein_g:.1f}g P, {i.carbs_g:.1f}g C, {i.fat_g:.1f}g F "
        f"({i.confidence} · {i.source})"
        for i in result.items
    )

    totals = result.running_totals
    deltas = result.delta_vs_target
    sign = lambda d, p=0: (f"+{d:.{p}f}" if d >= 0 else f"{d:.{p}f}")

    totals_html = (
        '<table style="font-size:13px; border-collapse:collapse; margin-top:8px;">'
        f'<tr><td style="padding:3px 12px 3px 0; color:#555;">Calories</td>'
        f'<td style="padding:3px 0;">{totals["kcal"]:.0f} / {targets["kcal"]} ({sign(deltas["kcal"])})</td></tr>'
        f'<tr><td style="padding:3px 12px 3px 0; color:#555;">Protein</td>'
        f'<td style="padding:3px 0;">{totals["protein_g"]:.1f}g / {targets["protein_g"]}g ({sign(deltas["protein_g"], 1)}g)</td></tr>'
        f'<tr><td style="padding:3px 12px 3px 0; color:#555;">Carbs</td>'
        f'<td style="padding:3px 0;">{totals["carbs_g"]:.1f}g / {targets["carbs_g"]}g ({sign(deltas["carbs_g"], 1)}g)</td></tr>'
        f'<tr><td style="padding:3px 12px 3px 0; color:#555;">Fat</td>'
        f'<td style="padding:3px 0;">{totals["fat_g"]:.1f}g / {targets["fat_g"]}g ({sign(deltas["fat_g"], 1)}g)</td></tr>'
        '</table>'
    )
    totals_text = (
        f"  Calories: {totals['kcal']:.0f} / {targets['kcal']} ({sign(deltas['kcal'])})\n"
        f"  Protein:  {totals['protein_g']:.1f}g / {targets['protein_g']}g ({sign(deltas['protein_g'], 1)}g)\n"
        f"  Carbs:    {totals['carbs_g']:.1f}g / {targets['carbs_g']}g ({sign(deltas['carbs_g'], 1)}g)\n"
        f"  Fat:      {totals['fat_g']:.1f}g / {targets['fat_g']}g ({sign(deltas['fat_g'], 1)}g)"
    )

    coach_note_html = ""
    coach_note_text = ""
    if result.coach_note:
        coach_note_html = (
            '<div style="background:#E8F0FE; border-left:3px solid #1F3A5F; '
            'padding:10px 14px; margin:16px 0; border-radius:2px; font-size:14px;">'
            f'<strong style="color:#1F3A5F;">Coach note:</strong> '
            f'{html_lib.escape(result.coach_note)}</div>'
        )
        coach_note_text = f"\nCoach note: {result.coach_note}\n"

    low_conf_items = [i for i in result.items if i.confidence == "low"]
    low_conf_html = ""
    low_conf_text = ""
    if low_conf_items:
        names = ", ".join(html_lib.escape(i.name) for i in low_conf_items)
        names_plain = ", ".join(i.name for i in low_conf_items)
        low_conf_html = (
            '<p style="font-size:13px; color:#8B6914; background:#FFF8E5; '
            'border-left:3px solid #E8A33D; padding:8px 12px; border-radius:2px; margin-top:14px;">'
            f'Low-confidence estimates: {names}. '
            'Reply with quantities if you want to refine them.</p>'
        )
        low_conf_text = (
            f"\nLow-confidence estimates: {names_plain}. "
            "Reply with quantities if you want to refine them.\n"
        )

    html = (
        f'<!DOCTYPE html><html><body style="{CSS_BASE}">'
        f'<div style="border-left:4px solid #1F3A5F; padding-left:14px; margin-bottom:18px;">'
        f'<div style="color:#555; font-size:13px; text-transform:uppercase; letter-spacing:0.5px;">Food logged</div>'
        f'<div style="font-size:20px; font-weight:600; color:#1F3A5F; margin-top:4px;">{date_str}</div>'
        f'</div>'
        f'<div style="font-size:14px; font-weight:600; color:#1F3A5F; margin-bottom:6px;">Logged</div>'
        f'<ul style="font-size:13px; color:#444; margin:0 0 14px 0; padding-left:20px;">{logged_rows_html}</ul>'
        f'<div style="font-size:14px; font-weight:600; color:#1F3A5F; margin-top:14px;">Today so far</div>'
        f'{totals_html}'
        f'{coach_note_html}'
        f'{low_conf_html}'
        f'</body></html>'
    )
    plain = (
        f"FOOD LOGGED — {date_str}\n"
        f"{'=' * 60}\n\n"
        f"Logged:\n{logged_rows_text}\n\n"
        f"Today so far:\n{totals_text}\n"
        f"{coach_note_text}"
        f"{low_conf_text}"
    )
    return html, plain


def _handle_mobility_log(text: str, message_id: str, from_addr: str) -> None:
    """Phase 1 stub: record the intent silently. No email sent until Phase 3 parsing lands."""
    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "mobility_log",
        "action": "logged_pending_parsing",
        "override_applied": False,
    })
    print("[mobility_log] Logged silently — no email sent until Phase 3.")


_RE_NUTRITION_KEYWORDS = re.compile(
    r"\b(protein|carb|carbs|kcal|calorie|calories|fat|macros?|nutrition|ate|food|meal|fuel)\b",
    re.IGNORECASE,
)


_RE_RUNNING_KEYWORDS = re.compile(
    r"\b(run|runs|running|ran|pace|paces|tempo|interval|intervals|easy run|long run|"
    r"marathon|threshold|km|kms|mileage|zone\s?2|hr|heart rate)\b",
    re.IGNORECASE,
)


def _handle_question(text: str, message_id: str, from_addr: str, profile) -> None:
    """Answer questions. Nutrition uses the food-log context; training/running questions
    route to the on-demand coach (advice only — writes no override)."""
    if not _RE_NUTRITION_KEYWORDS.search(text):
        domain = "run" if _RE_RUNNING_KEYWORDS.search(text) else "strength"
        today = datetime.now(TZ_AMSTERDAM).date()
        try:
            summary = ts.build_summary(days=14, today=today)
        except Exception as exc:  # noqa: BLE001 — best-effort context
            print(f"[warn] training summary unavailable: {exc}", file=sys.stderr)
            summary = ""
        try:
            load = weekly_load.build_weekly_load(days=7, today=today, profile_id=profile.id)
        except Exception as exc:  # noqa: BLE001 — best-effort context
            print(f"[warn] weekly load unavailable: {exc}", file=sys.stderr)
            load = None

        try:
            answer = coach_orchestrator.answer_training_question(
                text, domain=domain, training_summary=summary,
                profile=profile, weekly_load=load,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"[error] Training Q&A failed: {exc}", file=sys.stderr)
            _append_feedback_log({
                "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
                "message_id": message_id,
                "from_address": from_addr,
                "reply_text": text,
                "intent": "question",
                "error": str(exc),
                "override_applied": False,
            })
            _send_plain_notice("Question — couldn't answer", f"Error: {exc}")
            return

        _append_feedback_log({
            "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
            "message_id": message_id,
            "from_address": from_addr,
            "reply_text": text,
            "intent": "question",
            "action": "answered",
            "domain": domain,
            "override_applied": False,
        })
        _send_plain_notice(f"Re: {text[:60]}", answer)
        return

    today = datetime.now(TZ_AMSTERDAM).date()
    day_log = nutrition_logger.read_day(today)
    weekly = nutrition_logger.weekly_summary(days=7, targets=profile.daily_targets)

    try:
        answer = coach_orchestrator.answer_nutrition_question(
            text, day_log, profile.daily_targets, weekly, profile=profile,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"[error] Nutrition Q&A failed: {exc}", file=sys.stderr)
        _append_feedback_log({
            "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
            "message_id": message_id,
            "from_address": from_addr,
            "reply_text": text,
            "intent": "question",
            "error": str(exc),
            "override_applied": False,
        })
        _send_plain_notice("Question — couldn't answer", f"Error: {exc}")
        return

    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "question",
        "action": "answered",
        "override_applied": False,
    })
    _send_plain_notice(f"Re: {text[:60]}", answer)


def _handle_clarify(text: str, message_id: str, from_addr: str) -> None:
    """Classifier flagged the reply as ambiguous. Ask for a clearer reply."""
    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "none_clear",
        "action": "clarify_requested",
        "override_applied": False,
    })
    _send_plain_notice(
        "Couldn't classify your reply",
        "Wasn't sure if you meant training feedback, food log, mobility log, or a question. "
        "Try one of:\n"
        "  - 'change tomorrow's session to ...'\n"
        "  - 'food log: ...'\n"
        "  - 'mobility: 20 min, hips tight'\n"
        "  - or reply 'revert' to drop any override",
    )


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    for val, name in [
        (GMAIL_USER, "GMAIL_USER"),
        (GMAIL_PASSWORD, "GMAIL_APP_PASSWORD"),
        (RESEND_API_KEY, "RESEND_API_KEY"),
    ]:
        if not val:
            sys.exit(f"{name} env var not set.")

    global _ACTIVE_PROFILE_ID
    profile = default_profile()
    _ACTIVE_PROFILE_ID = profile.id
    state = store.get_state(profile.id)
    mode = state.get("mode", "normal")

    if mode == "paused":
        print("[skip] All emails paused.")
        return

    plan = _load_json(ROOT / "plan_template.json")
    overrides = _load_overrides()

    today_local = datetime.now(TZ_AMSTERDAM).date()
    coach_orchestrator.sync_taper_state(today_local)
    tomorrow = today_local + timedelta(days=1)

    print(f"[poll] Connecting to imap.gmail.com as {GMAIL_USER} ...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
    except Exception as exc:
        sys.exit(f"IMAP login failed: {exc}")

    mail.select("INBOX")
    _, data = mail.search(None, _imap_search_query(profile))
    msg_ids = data[0].split() if data[0] else []
    print(f"[poll] {len(msg_ids)} unread message(s) from {profile.email}")

    for msg_id in msg_ids:
        message_id = ""
        try:
            _, raw_data = mail.fetch(msg_id, "(RFC822)")
            msg = email_lib.message_from_bytes(raw_data[0][1])
            reply_text = _strip_quoted_history(_get_plain_body(msg))
            message_id = msg.get("Message-ID", "")
            from_addr = _decode_header(msg.get("From", ""))

            print(f"[msg] From: {from_addr}")
            print(f"[msg] Subject: {_decode_header(msg.get('Subject', ''))}")
            print(f"[msg] Body (stripped, first 200): {reply_text[:200]!r}")

            # 1. Mode commands work regardless of current mode (enter, exit, pause)
            new_mode = _detect_mode_command(reply_text)
            if new_mode is not None:
                _apply_mode_change(new_mode, state, today_local)
                mode = new_mode
                _send_plain_notice(f"Mode update: {new_mode}", _MODE_NOTICES[new_mode])
                _append_feedback_log({
                    "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
                    "message_id": message_id,
                    "from_address": from_addr,
                    "reply_text": reply_text,
                    "intent": f"mode_change:{new_mode}",
                    "action": f"mode_change:{new_mode}",
                    "override_applied": False,
                })
                continue

            # 2. A/B/C week-plan choice from Sunday summary
            m = _RE_WEEK_CHOICE.match(reply_text)
            if m:
                letter = m.group(1).upper()
                confirmation = _apply_week_choice(letter, state, today_local)
                if confirmation:
                    _send_plain_notice(f"Week plan: Option {letter}", confirmation)
                    print(f"[choice] Week option {letter} saved.")
                else:
                    _send_plain_notice(
                        "Week plan — no pending choice",
                        "No pending week plan found (or it has expired). "
                        "Wait for Sunday's summary to choose a plan.",
                    )
                _append_feedback_log({
                    "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
                    "message_id": message_id,
                    "from_address": from_addr,
                    "reply_text": reply_text,
                    "intent": f"week_choice:{letter}",
                    "action": f"week_choice:{letter}",
                    "override_applied": False,
                })
                continue

            # 3. Survival mode: ignore everything below this point
            if mode == "survival":
                print("[skip] Survival mode: ignored reply.")
                continue

            # 4. Revert — explicit, top-level. Bypasses the classifier so a misroute can't swallow it.
            if _RE_REVERT.search(reply_text):
                _handle_revert(reply_text, plan, overrides, tomorrow, message_id, from_addr)
                continue

            # 5. Intent classifier — splits compound replies into per-intent fragments.
            try:
                classification = intent_classifier.classify(reply_text, today=today_local)
            except (ValueError, RuntimeError) as exc:
                print(f"[error] Classifier failed: {exc}", file=sys.stderr)
                _append_feedback_log({
                    "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
                    "message_id": message_id,
                    "from_address": from_addr,
                    "reply_text": reply_text,
                    "intent": "classifier_error",
                    "error": str(exc),
                    "override_applied": False,
                })
                _send_plain_notice(
                    "Couldn't classify reply",
                    f"The intent classifier failed: {exc}\n\n"
                    "Reply 'revert' to drop any override, or rephrase your message.",
                )
                continue

            # 6. Dispatch per intent. Compound replies produce multiple handler calls.
            week_context = state.get("week_choice", "")
            for item in classification["intents"]:
                intent = item["intent"]
                text = item["text"]
                if intent == "training_feedback":
                    # The classifier resolves any day reference to an absolute ISO date
                    # in Amsterdam time; default to tomorrow when none was mentioned.
                    raw_target = item.get("target_date")
                    try:
                        target_day = date.fromisoformat(raw_target) if raw_target else tomorrow
                    except (TypeError, ValueError):
                        target_day = tomorrow
                    _handle_training_feedback(
                        text, plan, overrides, target_day, message_id, from_addr,
                        week_context, profile,
                    )
                elif intent == "food_log":
                    _handle_food_log(text, message_id, from_addr, profile)
                elif intent == "mobility_log":
                    _handle_mobility_log(text, message_id, from_addr)
                elif intent == "question":
                    _handle_question(text, message_id, from_addr, profile)
                elif intent == "none_clear":
                    _handle_clarify(text, message_id, from_addr)

        except Exception as exc:
            print(f"[error] Unhandled while processing message: {exc}", file=sys.stderr)
            _append_feedback_log({
                "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
                "message_id": message_id,
                "intent": "processing_error",
                "error": str(exc),
                "override_applied": False,
            })
            try:
                _send_plain_notice(
                    "Fitness bot — something went wrong",
                    f"Couldn't fully process your last reply: {exc}\n\n"
                    "Reply 'revert' to drop any override, or rephrase and try again.",
                )
            except Exception:
                pass
        finally:
            mail.store(msg_id, "+FLAGS", "\\Seen")

    mail.logout()

    _clean_old_overrides(overrides, today_local)


if __name__ == "__main__":
    main()
