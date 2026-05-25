#!/usr/bin/env python3
"""
Poll Gmail inbox for replies from Luke, process via Gemini, send replacement emails.

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

import gemini_client
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

# Patterns that trigger a phase transition rather than a Gemini call.
# Ordered from most specific to least so "phase 3" doesn't match "phase 2" prefix.
_PHASE_PATTERNS = [
    (r"\bswitch\s+to\s+phase\s*3\b|\bphase\s*3\b|\bready\s+for\s+phase\s*3\b|\bi.?m\s+ready\b", "phase3"),
    (r"\bswitch\s+to\s+phase\s*2\b|\bphase\s*2\b|\bbaby\s+born\b|\bbaby\s+arrived\b", "phase2"),
    (r"\bpause\b|\bpaused\b", "paused"),
]


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
    except Exception as exc:
        # Fallback: plain-text only if renderer fails (e.g. unexpected session shape)
        fallback_body = f"Coach note: {coach_note}\n\nSession: {json.dumps(session, indent=2)}"
        return (
            f"[Updated] Fitness plan — {date_str} — {session.get('session_type', '')}",
            f'<!DOCTYPE html><html><body style="{CSS_BASE}"><pre>{html_lib.escape(fallback_body)}</pre></body></html>',
            fallback_body,
        )

    # Inject coach note immediately after <body ...>
    body_tag_end = base_html.index(">", base_html.index("<body")) + 1
    full_html = base_html[:body_tag_end] + note_html + base_html[body_tag_end:]
    full_text = note_text + base_text

    subject = (
        f"[Updated] Fitness plan — {date_str} "
        f"(Day {day_num}) — {session['session_type']}"
    )
    return subject, full_html, full_text


# ──────────────────────────────────────────────────────────────────────────
# Phase transitions
# ──────────────────────────────────────────────────────────────────────────

def _detect_phase_transition(text: str) -> str | None:
    lower = text.lower()
    for pattern, phase in _PHASE_PATTERNS:
        if re.search(pattern, lower):
            return phase
    return None


def _apply_phase_transition(target_phase: str, state: dict, today_local: date) -> None:
    state["current_phase"] = target_phase
    if target_phase == "phase2" and not state.get("baby_birth_date"):
        state["baby_birth_date"] = today_local.isoformat()
        print(f"[phase] Set baby_birth_date to {today_local.isoformat()}")
    with open(ROOT / "state.json", "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    print(f"[phase] Transitioned to {target_phase}")


# ──────────────────────────────────────────────────────────────────────────
# Core message handler
# ──────────────────────────────────────────────────────────────────────────

def _process_message(
    mail: imaplib.IMAP4_SSL,
    msg_id: bytes,
    plan: dict,
    state: dict,
    overrides: dict,
    tomorrow: date,
) -> None:
    _, data = mail.fetch(msg_id, "(RFC822)")
    raw = data[0][1]
    msg = email_lib.message_from_bytes(raw)

    subject = _decode_header(msg.get("Subject", ""))
    from_addr = _decode_header(msg.get("From", ""))
    message_id = msg.get("Message-ID", "")
    full_body = _get_plain_body(msg)
    reply_text = _strip_quoted_history(full_body)

    print(f"[msg] From: {from_addr}")
    print(f"[msg] Subject: {subject}")
    print(f"[msg] Body (stripped, first 200): {reply_text[:200]!r}")

    today_local = datetime.now(TZ_AMSTERDAM).date()
    tomorrow_iso = tomorrow.isoformat()

    # ── Phase transition ──────────────────────────────────────────────────
    target_phase = _detect_phase_transition(reply_text)
    if target_phase:
        _apply_phase_transition(target_phase, state, today_local)
        mail.store(msg_id, "+FLAGS", "\\Seen")
        _send_plain_notice(
            f"Phase transition confirmed: {target_phase}",
            f"Done. Switched to {target_phase}.\n\n"
            "If this was a mistake, edit state.json directly and push.",
        )
        _append_feedback_log({
            "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
            "message_id": message_id,
            "from_address": from_addr,
            "reply_text": reply_text,
            "action": f"phase_transition:{target_phase}",
            "override_applied": False,
        })
        return

    # ── Revert ───────────────────────────────────────────────────────────
    if re.search(r"\brevert\b", reply_text, re.IGNORECASE):
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
            print(f"[revert] Removed override for {tomorrow_iso}, sent original session.")
        else:
            subj = f"[Revert] No override active for {tomorrow_iso}"
            html = (
                f'<!DOCTYPE html><html><body style="{CSS_BASE}">'
                f"<p>No override was active for {tomorrow_iso} — "
                "you're already on the template session.</p></body></html>"
            )
            text = f"No override active for {tomorrow_iso}."
            print(f"[revert] No override to revert for {tomorrow_iso}.")

        mail.store(msg_id, "+FLAGS", "\\Seen")
        _send_email(subj, html, text)
        _append_feedback_log({
            "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
            "message_id": message_id,
            "from_address": from_addr,
            "reply_text": reply_text,
            "action": "revert",
            "override_applied": False,
        })
        return

    # ── Normal feedback → Gemini ──────────────────────────────────────────
    current_session, _ = _get_current_session(tomorrow, plan, overrides)
    prev_override = overrides.get("overrides", {}).get(tomorrow_iso, {}).get("session")
    training_text = ts.build_summary(days=14)

    try:
        new_session = gemini_client.generate_session(
            reply_text=reply_text,
            current_session=current_session,
            recent_training_summary=training_text,
            previous_override=prev_override,
        )
    except (ValueError, RuntimeError) as exc:
        error_msg = str(exc)
        print(f"[error] Gemini failed: {error_msg}", file=sys.stderr)
        _append_feedback_log({
            "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
            "message_id": message_id,
            "from_address": from_addr,
            "reply_text": reply_text,
            "error": error_msg,
            "override_applied": False,
        })
        mail.store(msg_id, "+FLAGS", "\\Seen")
        _send_plain_notice(
            "Fitness bot — couldn't process your feedback",
            f"Couldn't process your feedback. Error:\n\n{error_msg}\n\n"
            "Fix the issue and try again, or reply with 'revert' to stay on the template session.",
        )
        return

    # Write override
    overrides.setdefault("overrides", {})[tomorrow_iso] = {
        "applied_at": datetime.now(TZ_AMSTERDAM).isoformat(),
        "feedback_source": f"reply: {reply_text[:200]!r}",
        "session": new_session,
    }

    _append_feedback_log({
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
        "message_id": message_id,
        "from_address": from_addr,
        "reply_text": reply_text,
        "coach_note": new_session.get("coach_note", ""),
        "target_date": tomorrow_iso,
        "override_applied": True,
    })

    coach_note = new_session.get("coach_note", "")
    subj, html, text = _build_replacement_email(new_session, coach_note, tomorrow, plan)
    ok = _send_email(subj, html, text)
    mail.store(msg_id, "+FLAGS", "\\Seen")
    if ok:
        print(f"[ok] Replacement email sent for {tomorrow_iso}.")
    else:
        print(f"[error] Replacement email send failed for {tomorrow_iso}.", file=sys.stderr)


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
    phase = state.get("current_phase", "phase1")

    if phase == "phase2":
        print("[skip] Phase 2 active — feedback loop disabled. Switching phases via email still works.")
        # Still check for phase-transition commands even in phase2
    elif phase == "paused":
        print("[skip] Phase paused — feedback loop disabled.")
        return

    plan = _load_json(ROOT / "plan_template.json")
    overrides = _load_overrides()

    today_local = datetime.now(TZ_AMSTERDAM).date()
    tomorrow = today_local + timedelta(days=1)

    print(f"[poll] Connecting to imap.gmail.com as {GMAIL_USER} ...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
    except Exception as exc:
        sys.exit(f"IMAP login failed: {exc}")

    mail.select("INBOX")
    _, data = mail.search(None, f'UNSEEN FROM "levans092@gmail.com"')
    msg_ids = data[0].split() if data[0] else []
    print(f"[poll] {len(msg_ids)} unread message(s) from levans092@gmail.com")

    for msg_id in msg_ids:
        if phase == "phase2":
            # In phase2 only handle phase-transition commands; ignore training feedback
            _, raw_data = mail.fetch(msg_id, "(RFC822)")
            msg = email_lib.message_from_bytes(raw_data[0][1])
            reply_text = _strip_quoted_history(_get_plain_body(msg))
            target_phase = _detect_phase_transition(reply_text)
            if target_phase:
                _apply_phase_transition(target_phase, state, today_local)
                mail.store(msg_id, "+FLAGS", "\\Seen")
                _send_plain_notice(
                    f"Phase transition confirmed: {target_phase}",
                    f"Done. Switched to {target_phase}.",
                )
                _append_feedback_log({
                    "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(),
                    "message_id": msg.get("Message-ID", ""),
                    "from_address": _decode_header(msg.get("From", "")),
                    "reply_text": reply_text,
                    "action": f"phase_transition:{target_phase}",
                    "override_applied": False,
                })
            else:
                mail.store(msg_id, "+FLAGS", "\\Seen")
                print(f"[skip] Phase 2: ignored non-transition message.")
        else:
            _process_message(mail, msg_id, plan, state, overrides, tomorrow)

    mail.logout()

    _clean_old_overrides(overrides)
    _save_overrides(overrides)


if __name__ == "__main__":
    main()
