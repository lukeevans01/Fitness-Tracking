#!/usr/bin/env python3
"""Execute one Telegram update. Entry point for the telegram-inbound workflow.

The update arrives as JSON in TELEGRAM_UPDATE (the webhook forwards it via
repository_dispatch). telegram_router decides what it means; this module does it and
replies in the chat.

Deliberately reuses the same store, guardrails and coach as the email path. Only the
transport differs, so the two channels cannot give different answers.

Env:
  TELEGRAM_UPDATE       the raw update JSON
  TELEGRAM_BOT_TOKEN    to reply
  TELEGRAM_CHAT_ID      the only chat that may drive the bot
  GEMINI_API_KEY        needed for questions and free-text feedback, not for buttons
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import notify_telegram
import store
import telegram_router as router
from profile import default_profile

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

FEEDBACK_LABELS = {
    "done": "Done as prescribed",
    "skip": "Skipped",
    "hard": "Too hard / cut short",
}


def _today() -> date:
    return datetime.now(TZ_AMSTERDAM).date()


def _already_handled(update_id: "int | None", profile_id: str) -> bool:
    """True if this update has been processed before.

    Telegram retries a webhook until it gets a 200, and a repository_dispatch can be
    replayed, so the same tap could arrive twice. Applying a week twice is harmless but
    logging the same feedback twice is noise, so updates are recorded and skipped.
    """
    if update_id is None:
        return False
    adaptation = store.get_adaptation(profile_id)
    seen = adaptation.get("telegram_seen_updates") or []
    if update_id in seen:
        return True
    # Keep a short tail; Telegram update ids only ever increase.
    store.set_adaptation(profile_id, {"telegram_seen_updates": ([*seen, update_id])[-50:]})
    return False


def _handle_week_choice(action, profile) -> str:
    import process_replies

    process_replies._ACTIVE_PROFILE_ID = profile.id
    state = store.get_state(profile.id)
    confirmation = process_replies._apply_week_choice(action.letter, state, _today())
    if not confirmation:
        return (
            f"No week is currently open for option {action.letter}. The Sunday review sets "
            "the options; they expire after a week."
        )
    return confirmation


def _handle_session_feedback(action, profile) -> str:
    """Record how a session went. Deliberately does not rewrite the plan.

    A tap is a log, not an instruction: 'skipped' should not silently re-plan the week. The
    Sunday review picks these up as context when it calibrates.
    """
    label = FEEDBACK_LABELS.get(action.feedback, action.feedback)
    store.append_feedback(profile.id, {
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds"),
        "source": "telegram_button",
        "intent": f"session_feedback:{action.feedback}",
        "target_date": action.iso_date,
        "reply_text": label,
    })
    when = date.fromisoformat(action.iso_date).strftime("%a %d %b")
    return f"Logged for {when}: {label.lower()}. It will feed into Sunday's review."


def _handle_mode_change(action, profile) -> str:
    import process_replies

    process_replies._ACTIVE_PROFILE_ID = profile.id
    mode = action.meta.get("mode", "normal")
    state = store.get_state(profile.id)
    process_replies._apply_mode_change(mode, state, _today())
    return process_replies._MODE_NOTICES.get(mode, f"Mode set to {mode}.")


def _handle_question(action, profile) -> str:
    import coach_orchestrator
    import training_summary as ts
    import weekly_load

    today = _today()
    try:
        load = weekly_load.build_weekly_load(days=7, today=today, profile_id=profile.id)
    except Exception:
        load = None
    answer = coach_orchestrator.answer_training_question(
        question=action.text,
        profile=profile,
        training_summary=ts.build_summary(days=14, today=today),
        weekly_load=load,
    )
    return answer if isinstance(answer, str) else str(answer)


def _handle_training_feedback(action, profile) -> str:
    """Adjust tomorrow's session from free text, mirroring the email reply path."""
    import coach_orchestrator
    import process_replies
    import training_summary as ts
    import weekly_load

    process_replies._ACTIVE_PROFILE_ID = profile.id
    today = _today()
    target = today + timedelta(days=1)
    plan = json.loads((ROOT / "plan_template.json").read_text(encoding="utf-8"))
    overrides = {"overrides": store.get_overrides(profile.id)}
    current_session, _ = process_replies._get_current_session(target, plan, overrides)

    try:
        load = weekly_load.build_weekly_load(days=7, today=today, profile_id=profile.id)
    except Exception:
        load = None

    new_session = coach_orchestrator.generate_session(
        reply_text=action.text,
        current_session=current_session,
        domain=coach_orchestrator.infer_domain(current_session.get("session_kind", "strength")),
        profile=profile,
        training_summary=ts.build_summary(days=14, today=today),
        weekly_load=load,
    )

    store.set_override(profile.id, target.isoformat(), {
        "applied_at": datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds"),
        "feedback_source": f"telegram: {action.text[:200]!r}",
        "session": new_session,
    })
    store.append_feedback(profile.id, {
        "timestamp": datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds"),
        "source": "telegram",
        "intent": "training_feedback",
        "target_date": target.isoformat(),
        "reply_text": action.text,
        "override_applied": True,
    })

    note = new_session.get("coach_note", "")
    return (
        f"{target:%a %d %b} updated: {new_session.get('session_type', '?')}"
        f"{chr(10) + chr(10) + note if note else ''}"
    )


def _handle_food_log(action, profile) -> str:
    import nutrition_logger

    try:
        result = nutrition_logger.log_food(action.text, _today(), profile=profile)
    except Exception as exc:
        print(f"[error] food log failed: {exc}", file=sys.stderr)
        return "Could not parse that as food. Try naming the items and rough amounts."

    if not getattr(result, "items", None):
        return "Logged, but nothing recognisable was parsed out of it."
    totals = getattr(result, "running_totals", None) or {}
    note = getattr(result, "coach_note", "") or ""
    line = (
        f"Logged {len(result.items)} item(s). Today so far: "
        f"{totals.get('kcal', 0):.0f} kcal, {totals.get('protein_g', 0):.0f}g protein "
        f"(targets {profile.daily_targets['kcal']} kcal, "
        f"{profile.daily_targets['protein_g']}g)."
    )
    return f"{line}\n\n{note}".strip()


HANDLERS = {
    router.WEEK_CHOICE: _handle_week_choice,
    router.SESSION_FEEDBACK: _handle_session_feedback,
    router.MODE_CHANGE: _handle_mode_change,
    router.QUESTION: _handle_question,
    router.TRAINING_FEEDBACK: _handle_training_feedback,
    router.FOOD_LOG: _handle_food_log,
}


def main() -> int:
    raw = os.environ.get("TELEGRAM_UPDATE") or ""
    if not raw.strip():
        print("TELEGRAM_UPDATE is empty; nothing to do.")
        return 0
    try:
        update = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"TELEGRAM_UPDATE is not valid JSON: {exc}", file=sys.stderr)
        return 1

    profile = default_profile()
    action = router.route(update, os.environ.get("TELEGRAM_CHAT_ID"))
    print(f"[router] {action.kind}" + (f" ({action.reason})" if action.reason else ""))

    # Stop the button spinner before doing any slow work.
    if action.callback_id:
        notify_telegram.answer_callback_query(action.callback_id, "Working on it...")

    if action.kind == router.UNAUTHORISED:
        # Never reply: replying would confirm the bot is live to whoever probed it.
        print(f"[drop] unauthorised update: {action.reason}", file=sys.stderr)
        return 0
    if action.kind == router.IGNORE:
        return 0
    if action.kind == router.HELP:
        notify_telegram.send_message(router.help_text())
        return 0

    if _already_handled(action.update_id, profile.id):
        print(f"[skip] update {action.update_id} already handled.")
        return 0

    handler = HANDLERS.get(action.kind)
    if not handler:
        notify_telegram.send_message(router.help_text())
        return 0

    try:
        reply = handler(action, profile)
    except Exception as exc:
        print(f"[error] {action.kind} failed: {exc}", file=sys.stderr)
        notify_telegram.send_message(
            f"Something went wrong handling that ({type(exc).__name__}). "
            "Nothing has been changed."
        )
        return 1

    # A used option should not be tappable twice.
    if action.kind == router.WEEK_CHOICE and action.message_id:
        notify_telegram.clear_buttons(action.message_id)

    notify_telegram.send_message(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
