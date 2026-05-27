#!/usr/bin/env python3
"""
Poll Gmail inbox for replies from Luke, classify intent, dispatch to a handler.

Env vars required:
  GMAIL_USER           Bot Gmail address (e.g. luke.fitness.bot@gmail.com)
  GMAIL_APP_PASSWORD   16-char App Password (no spaces)
  GEMINI_API_KEY       Gemini API key
  RESEND_API_KEY       Resend API key
  TO_EMAIL             Luke's email (default: levans092@gmail.com)
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
import training_summary as ts
from send_daily import (
    CSS_BASE,
    build_phase1_html,
    build_phase1_text,
)

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

GMAIL_USER = os.environ.get("GMAIL_USER") or ""
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD") or ""
RESEND_API_KEY = os.environ.get("RESEND_API_KEY") or ""
TO_EMAIL = os.environ.get("TO_EMAIL") or "levans092@gmail.com"
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

_MODE_NOTICES = {
    "survival": (
        "Survival mode active. Daily emails paused.\n\n"
        "Reply 'I'm back' or 'resume training' when you're ready to pick up training again."
    ),
    "normal": (
        "Back in training. Daily emails will resume tonight.\n\n"
        "Training continues toward sub-3:25 at San Sebastián (22 Nov 2026)."
    ),
    "paused": (
        "All emails paused. Edit state.json and set mode to 'normal' to resume."
    ),
}


# ──────────────────────────────────────────────────────────────────────────
# File I/O
# ──────────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _load_overrides() -> dict:
    path = ROOT / "overrides.json"
    if not path.exists():
        return {"overrides": {}}
    data = _load_json(path)
    data.setdefault("overrides", {})
    return data


def _save_overrides(overrides: dict) -> None:
    overrides["_comment"] = (
        "Per-date session overrides from feedback replies. "
        "Auto-cleaned of entries older than 7 days on each run."
    )
    with open(ROOT / "overrides.json", "w") as f:
        json.dump(overrides, f, indent=2)
        f.write("\n")


def _save_state(state: dict) -> None:
    with open(ROOT / "state.json", "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def _append_feedback_log(entry: dict) -> None:
    with open(ROOT / "feedback_log.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def _clean_old_overrides(overrides: dict) -> None:
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    stale = [k for k in overrides.get("overrides", {}) if k < cutoff]
    for k in stale:
        del overrides["overrides"][k]
    if stale:
        print(f"[cleanup] Removed {len(stale)} stale override(s): {stale}")


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
    """Return (day_num, session, day_after_session) for target_date from the phase1 cycle."""
    start = date.fromisoformat(plan["phase1_start_date"])
    cycle = plan["phase1_cycle_length_days"]
    days_in = (target_date - start).days
    day_num = (days_in % cycle) + 1
    day_after_num = ((days_in + 1) % cycle) + 1
    session = next(d for d in plan["phase1_days"] if d["day_num"] == day_num)
    day_after = next(d for d in plan["phase1_days"] if d["day_num"] == day_after_num)
    return day_num, session, day_after


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
    # Keep current_phase in sync so send_daily.py pauses/resumes correctly
    if new_mode in ("survival", "paused"):
        state["current_phase"] = "paused"
    elif new_mode == "normal":
        state["current_phase"] = "phase1"
    _save_state(state)
    _update_adaptation_state_mode(new_mode, prev_mode, today_local)
    print(f"[mode] {prev_mode} → {new_mode}")


def _update_adaptation_state_mode(new_mode: str, prev_mode: str, today_local: date) -> None:
    path = ROOT / "adaptation_state.md"
    if not path.exists():
        return
    today_iso = today_local.isoformat()
    content = path.read_text()
    content = re.sub(r"(?m)^mode: \S+", f"mode: {new_mode}", content)
    content = re.sub(r"(?m)^last_updated: \S+", f"last_updated: {today_iso}", content)
    if new_mode == "survival" and prev_mode != "survival":
        # Replace placeholder row with real entry
        content = content.replace("| — | — | — |", f"| {today_iso} | — | — |", 1)
    elif new_mode == "normal" and prev_mode == "survival":
        # Close the most recent open survival entry
        content = re.sub(
            r"(\| \d{4}-\d{2}-\d{2}) \| — \| — \|",
            rf"\1 | {today_iso} | — |",
            content,
            count=1,
        )
    path.write_text(content)


# ──────────────────────────────────────────────────────────────────────────
# Week-plan choice (A/B/C from Sunday summary)
# ──────────────────────────────────────────────────────────────────────────

def _apply_week_choice(letter: str, state: dict, today_local: date) -> str | None:
    """Record Luke's A/B/C pick. Returns confirmation text, or None if no valid pending choice."""
    path = ROOT / "plans" / "pending-choice.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            pending = json.load(f)
    except (json.JSONDecodeError, OSError):
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
    with open(path, "w") as f:
        json.dump(pending, f, indent=2)
        f.write("\n")
    return (
        f"Week plan confirmed: Option {letter_upper} — {opt['label']}\n\n"
        f"{opt['sessions']}\n\n"
        f"{opt['rationale']}"
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
    hard_rules = plan["hard_rules_phase1"]

    note_html = (
        '<div style="background:#E8F0FE; border-left:3px solid #1F3A5F; '
        'padding:10px 14px; margin:0 0 20px 0; border-radius:2px;">'
        f'<strong style="color:#1F3A5F;">Coach note:</strong> '
        f"{html_lib.escape(coach_note)}</div>"
    )
    note_text = f"[Updated] Coach note: {coach_note}\n\n"

    try:
        base_html = build_phase1_html(session, day_after, day_num, target_date, hard_rules)
        base_text = build_phase1_text(session, day_after, day_num, target_date, hard_rules)
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
        f"(Day {day_num}) — {session['session_type']}"
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
        del overrides["overrides"][tomorrow_iso]
        original_session, day_num = _get_current_session(tomorrow, plan, overrides)
        _, _, day_after = _cycle_day(tomorrow, plan)
        date_str = tomorrow.strftime("%a %d %b")
        hard_rules = plan["hard_rules_phase1"]
        html = build_phase1_html(original_session, day_after, day_num, tomorrow, hard_rules)
        text = build_phase1_text(original_session, day_after, day_num, tomorrow, hard_rules)
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
    tomorrow: date,
    message_id: str,
    from_addr: str,
    week_context: str,
) -> None:
    """Send the training-feedback fragment to the coach orchestrator and apply the override."""
    tomorrow_iso = tomorrow.isoformat()
    current_session, _ = _get_current_session(tomorrow, plan, overrides)
    prev_override = overrides.get("overrides", {}).get(tomorrow_iso, {}).get("session")
    training_text = ts.build_summary(days=14)

    domain = coach_orchestrator.infer_domain(current_session.get("session_kind", "strength"))
    try:
        new_session = coach_orchestrator.generate_session(
            domain=domain,
            reply_text=text,
            current_session=current_session,
            training_summary=training_text,
            previous_override=prev_override,
            week_context=week_context,
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

    overrides.setdefault("overrides", {})[tomorrow_iso] = {
        "applied_at": datetime.now(TZ_AMSTERDAM).isoformat(),
        "feedback_source": f"reply: {text[:200]!r}",
        "session": new_session,
    }

    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "training_feedback",
        "coach_note": new_session.get("coach_note", ""),
        "target_date": tomorrow_iso,
        "override_applied": True,
    })

    coach_note = new_session.get("coach_note", "")
    subj, html, body_text = _build_replacement_email(new_session, coach_note, tomorrow, plan)
    ok = _send_email(subj, html, body_text)
    if ok:
        print(f"[ok] Replacement email sent for {tomorrow_iso}.")
    else:
        print(f"[error] Replacement email send failed for {tomorrow_iso}.", file=sys.stderr)


def _handle_food_log(text: str, message_id: str, from_addr: str) -> None:
    """Phase 1 stub: log the intent, send acknowledgement. Real parsing in Phase 2."""
    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "food_log",
        "action": "logged_pending_phase2",
        "override_applied": False,
    })
    _send_plain_notice(
        "Food log noted",
        "Got your food log — parsing isn't wired in yet (Phase 2). "
        "I've recorded the reply for when the nutrition coach goes live.",
    )


def _handle_mobility_log(text: str, message_id: str, from_addr: str) -> None:
    """Phase 1 stub: log the intent, send acknowledgement. Real parsing in Phase 3."""
    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "mobility_log",
        "action": "logged_pending_phase3",
        "override_applied": False,
    })
    _send_plain_notice(
        "Mobility log noted",
        "Got your mobility note — parsing isn't wired in yet (Phase 3). "
        "I've recorded the reply for the consistency tracker.",
    )


def _handle_question(text: str, message_id: str, from_addr: str) -> None:
    """Phase 1 stub: log + acknowledge. Real Q&A in Phase 2/3."""
    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": text,
        "intent": "question",
        "action": "logged_pending_qa",
        "override_applied": False,
    })
    _send_plain_notice(
        "Question noted",
        "Got your question — I can't answer Q&A in this phase. "
        "Reply with a specific change request (e.g. 'drop tomorrow's volume') if you want a session adjustment.",
    )


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

    state = _load_json(ROOT / "state.json")
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
    _, data = mail.search(None, 'UNSEEN FROM "levans092@gmail.com"')
    msg_ids = data[0].split() if data[0] else []
    print(f"[poll] {len(msg_ids)} unread message(s) from levans092@gmail.com")

    for msg_id in msg_ids:
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
            mail.store(msg_id, "+FLAGS", "\\Seen")
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
            mail.store(msg_id, "+FLAGS", "\\Seen")
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

        # 3. Survival mode: ignore everything below this point (preserves existing behaviour)
        if mode == "survival":
            mail.store(msg_id, "+FLAGS", "\\Seen")
            print("[skip] Survival mode: ignored reply.")
            continue

        # 4. Revert — explicit, top-level. Bypasses the classifier so a misroute can't swallow it.
        if _RE_REVERT.search(reply_text):
            _handle_revert(reply_text, plan, overrides, tomorrow, message_id, from_addr)
            mail.store(msg_id, "+FLAGS", "\\Seen")
            continue

        # 5. Intent classifier — splits compound replies into per-intent fragments.
        try:
            classification = intent_classifier.classify(reply_text)
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
            mail.store(msg_id, "+FLAGS", "\\Seen")
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
                _handle_training_feedback(
                    text, plan, overrides, tomorrow, message_id, from_addr, week_context,
                )
            elif intent == "food_log":
                _handle_food_log(text, message_id, from_addr)
            elif intent == "mobility_log":
                _handle_mobility_log(text, message_id, from_addr)
            elif intent == "question":
                _handle_question(text, message_id, from_addr)
            elif intent == "none_clear":
                _handle_clarify(text, message_id, from_addr)

        mail.store(msg_id, "+FLAGS", "\\Seen")

    mail.logout()

    _clean_old_overrides(overrides)
    _save_overrides(overrides)


if __name__ == "__main__":
    main()
