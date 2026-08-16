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
import plan_guardrails
import store
import telegram_router as router
from profile import default_profile

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

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


# What a tap means, and what the coach should do about it.
FEEDBACK_LABELS = {
    "done": "Done as prescribed",
    "skip": "Skipped",
    "hard": "Too hard / cut short",
}
# Instruction sent to the coach. "done" is absent on purpose: adherence needs no repair, and
# inventing a change would make the plan wander for no reason.
FEEDBACK_INSTRUCTIONS = {
    "skip": (
        "I skipped the {session} that was scheduled for {when}. Adjust the next session so "
        "the week still works: decide whether to absorb the loss or pick up what matters "
        "most from the missed one, and do not simply pile it on top."
    ),
    "hard": (
        "The {session} on {when} was too hard and I cut it short. Ease the next session and "
        "say what you changed. Do not increase anything."
    ),
}


def _adjust_next_session(reply_text: str, profile, intent: str) -> "tuple[str, dict | None]":
    """Have the coach revise tomorrow's session from `reply_text`, and store it.

    Returns (message, new_session). Tomorrow is the target because it is the next thing that
    can still be changed, which is the same day the email reply path adjusts.
    """
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
        reply_text=reply_text,
        current_session=current_session,
        domain=coach_orchestrator.infer_domain(current_session.get("session_kind", "strength")),
        profile=profile,
        training_summary=ts.build_summary(days=14, today=today),
        weekly_load=load,
    )

    problem = _session_problem(new_session)
    if problem:
        # A single day is not covered by the weekly guardrails, so the shape is checked here
        # rather than writing something malformed into the plan.
        print(f"[reject] coach returned an unusable session: {problem}", file=sys.stderr)
        return (f"The coach's revision did not look right ({problem}), so {target:%a %d %b} "
                "is unchanged."), None

    now = datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds")
    store.set_override(profile.id, target.isoformat(), {
        "applied_at": now,
        "feedback_source": f"telegram: {reply_text[:200]!r}",
        "session": new_session,
    })
    store.append_feedback(profile.id, {
        "timestamp": now,
        "source": "telegram",
        "intent": intent,
        "target_date": target.isoformat(),
        "reply_text": reply_text,
        "override_applied": True,
    })

    note = (new_session.get("coach_note") or "").strip()
    message = f"{target:%a %d %b} updated: {new_session.get('session_type', '?')}"
    return (f"{message}\n\n{note}" if note else message), new_session


def _session_problem(session: object) -> str:
    """Return a reason the session is unusable, or an empty string if it is fine."""
    if not isinstance(session, dict):
        return "not an object"
    if session.get("session_kind") not in plan_guardrails.VALID_KINDS:
        return f"session_kind {session.get('session_kind')!r}"
    if not session.get("session_type"):
        return "no session_type"
    duration = session.get("duration_min")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return "duration_min is not a number"
        if duration < 0 or duration > plan_guardrails.MAX_SESSION_MINUTES:
            return f"duration_min {duration}"
    return ""


def _handle_session_feedback(action, profile) -> str:
    """Log how a session went and, where there is something to fix, act on it.

    "Done" needs no repair, so it is recorded and nothing moves. "Skipped" and "too hard"
    both say the plan and reality have diverged, so the coach revises tomorrow.
    """
    label = FEEDBACK_LABELS.get(action.feedback, action.feedback)
    when = date.fromisoformat(action.iso_date).strftime("%a %d %b")
    now = datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds")

    instruction = FEEDBACK_INSTRUCTIONS.get(action.feedback)
    if not instruction:
        store.append_feedback(profile.id, {
            "timestamp": now,
            "source": "telegram_button",
            "intent": f"session_feedback:{action.feedback}",
            "target_date": action.iso_date,
            "reply_text": label,
        })
        return (
            f"Logged for {when}: {label.lower()}. Nothing to change, so the plan stands. "
            "It feeds into Sunday's review."
        )

    session_name = _session_name_for(action.iso_date, profile)
    reply_text = instruction.format(session=session_name, when=when)
    message, _ = _adjust_next_session(
        reply_text, profile, intent=f"session_feedback:{action.feedback}"
    )
    return f"Logged for {when}: {label.lower()}.\n\n{message}"


def _session_name_for(iso_date: str, profile) -> str:
    """The session that was scheduled on `iso_date`, for the coach's context."""
    import process_replies

    process_replies._ACTIVE_PROFILE_ID = profile.id
    try:
        plan = json.loads((ROOT / "plan_template.json").read_text(encoding="utf-8"))
        overrides = {"overrides": store.get_overrides(profile.id)}
        session, _ = process_replies._get_current_session(
            date.fromisoformat(iso_date), plan, overrides
        )
        return session.get("session_type") or "session"
    except Exception:
        return "session"


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
    message, _ = _adjust_next_session(action.text, profile, intent="training_feedback")
    return message


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
