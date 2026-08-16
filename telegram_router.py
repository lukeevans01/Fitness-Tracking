#!/usr/bin/env python3
"""Parse a Telegram update into an action, with no side effects.

Pure and deterministic: no network, no LLM, no store writes. Everything that decides *what
an incoming message means* lives here so it can be tested exhaustively, and the handler is
left with only the doing.

Security note: authorisation is checked here as well as at the webhook. Anyone can find a
bot by username and message it, so an update from an unexpected chat must be dropped rather
than acted on. Defence in depth, because the webhook and this module can be deployed
independently.

Callback data is capped at 64 bytes by Telegram, so it stays terse:
    wk:B                     pick option B for the coming week
    fb:2026-08-17:done       mark that day done / skipped / hard
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Action kinds the handler knows how to execute.
WEEK_CHOICE = "week_choice"
SESSION_FEEDBACK = "session_feedback"
MODE_CHANGE = "mode_change"
TRAINING_FEEDBACK = "training_feedback"
QUESTION = "question"
FOOD_LOG = "food_log"
HELP = "help"
IGNORE = "ignore"
UNAUTHORISED = "unauthorised"

FEEDBACK_KINDS = {"done", "skip", "hard"}

_RE_WEEK_CB = re.compile(r"^wk:([ABC])$")
_RE_FEEDBACK_CB = re.compile(r"^fb:(\d{4}-\d{2}-\d{2}):(done|skip|hard)$")

# Text commands. Mirrors process_replies so the two channels behave the same way.
_RE_SURVIVAL_ENTER = re.compile(
    r"\bsurvival\s+mode\b|\bpause\s+training\b|\bbaby\s+born\b|\bbaby\s+arrived\b", re.IGNORECASE
)
_RE_SURVIVAL_EXIT = re.compile(r"\bi.?m\s+back\b|\bresume\s+training\b", re.IGNORECASE)
_RE_PAUSE_ALL = re.compile(r"^\s*pause\s*$", re.IGNORECASE)
_RE_WEEK_LETTER = re.compile(r"^\s*([ABC])\s*[.!?]?\s*$", re.IGNORECASE)
_RE_HELP = re.compile(r"^\s*/?(help|start|commands)\s*$", re.IGNORECASE)
_RE_QUESTION = re.compile(r"\?\s*$")

# Words that suggest food rather than training, used only to split free text.
_FOOD_HINTS = re.compile(
    r"\b(ate|eaten|eating|breakfast|lunch|dinner|snack|meal|kcal|calories|protein|"
    r"porridge|chicken|rice|eggs?|shake|yoghurt|yogurt)\b",
    re.IGNORECASE,
)


@dataclass
class Action:
    """What the handler should do about one update."""

    kind: str
    text: str = ""
    letter: str = ""
    iso_date: str = ""
    feedback: str = ""
    callback_id: str = ""
    message_id: "int | None" = None
    update_id: "int | None" = None
    chat_id: str = ""
    reason: str = ""
    meta: dict = field(default_factory=dict)


def _chat_of(update: dict) -> dict:
    message = update.get("message") or update.get("edited_message") or {}
    if message:
        return message.get("chat") or {}
    callback = update.get("callback_query") or {}
    return ((callback.get("message") or {}).get("chat")) or {}


def route(update: object, expected_chat_id: "str | None") -> Action:
    """Turn one Telegram update into an Action.

    expected_chat_id gates the update. None means "unconfigured", which is treated as
    closed rather than open: with no expectation set, nothing is authorised.
    """
    if not isinstance(update, dict):
        return Action(IGNORE, reason="update is not an object")

    update_id = update.get("update_id")
    chat = _chat_of(update)
    chat_id = str(chat.get("id", "")) if chat.get("id") is not None else ""

    if not expected_chat_id:
        return Action(UNAUTHORISED, update_id=update_id, chat_id=chat_id,
                      reason="no expected chat id configured")
    if chat_id != str(expected_chat_id):
        return Action(UNAUTHORISED, update_id=update_id, chat_id=chat_id,
                      reason=f"unexpected chat {chat_id!r}")

    callback = update.get("callback_query")
    if callback:
        return _route_callback(callback, update_id, chat_id)

    message = update.get("message") or update.get("edited_message")
    if not message:
        return Action(IGNORE, update_id=update_id, chat_id=chat_id,
                      reason="no message or callback on the update")

    text = (message.get("text") or "").strip()
    if not text:
        return Action(IGNORE, update_id=update_id, chat_id=chat_id,
                      reason="message carries no text")

    return _route_text(text, message, update_id, chat_id)


def _route_callback(callback: dict, update_id, chat_id: str) -> Action:
    data = (callback.get("data") or "").strip()
    callback_id = callback.get("id") or ""
    message_id = (callback.get("message") or {}).get("message_id")
    common = dict(callback_id=callback_id, message_id=message_id,
                  update_id=update_id, chat_id=chat_id)

    week = _RE_WEEK_CB.match(data)
    if week:
        return Action(WEEK_CHOICE, letter=week.group(1).upper(), **common)

    feedback = _RE_FEEDBACK_CB.match(data)
    if feedback:
        return Action(SESSION_FEEDBACK, iso_date=feedback.group(1),
                      feedback=feedback.group(2), **common)

    return Action(IGNORE, reason=f"unrecognised callback data {data!r}", **common)


def _route_text(text: str, message: dict, update_id, chat_id: str) -> Action:
    common = dict(text=text, message_id=message.get("message_id"),
                  update_id=update_id, chat_id=chat_id)

    if _RE_HELP.match(text):
        return Action(HELP, **common)

    if _RE_PAUSE_ALL.match(text):
        return Action(MODE_CHANGE, meta={"mode": "paused"}, **common)
    if _RE_SURVIVAL_ENTER.search(text):
        return Action(MODE_CHANGE, meta={"mode": "survival"}, **common)
    if _RE_SURVIVAL_EXIT.search(text):
        return Action(MODE_CHANGE, meta={"mode": "normal"}, **common)

    letter = _RE_WEEK_LETTER.match(text)
    if letter:
        return Action(WEEK_CHOICE, letter=letter.group(1).upper(), **common)

    # Free text. A trailing question mark means advice; food words mean a log; anything
    # else is treated as feedback on the plan, matching the email behaviour.
    if _RE_QUESTION.search(text):
        return Action(QUESTION, **common)
    if _FOOD_HINTS.search(text):
        return Action(FOOD_LOG, **common)
    return Action(TRAINING_FEEDBACK, **common)


def help_text() -> str:
    """What the bot can do, for /help and for an unrecognised command."""
    return (
        "What I can do:\n\n"
        "Tap a button on the Sunday review to pick your week. On a daily session, tap how it "
        "went: Skipped or Too hard sends the coach to revise tomorrow, Done just records it.\n\n"
        "Or just type:\n"
        "  A, B or C - pick this week's plan\n"
        "  a question ending in '?' - training advice, changes nothing\n"
        "  what you ate - logs food\n"
        "  anything else - treated as feedback on tomorrow's session\n"
        "  'survival mode' - pause the programme\n"
        "  \"I'm back\" - resume it\n"
        "  'pause' - stop all messages"
    )
