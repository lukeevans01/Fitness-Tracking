#!/usr/bin/env python3
"""Classify a Luke reply into one or more intents and split text accordingly.

Single entry point: classify(reply_text) -> {"intents": [{"intent": ..., "text": ...}, ...]}

Used by process_replies.py to dispatch a freeform reply to the right handler
(training feedback, food log, mobility log, question, or clarify).
"""

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gemini_client

_TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

# A target_date may reference a day up to this many days out; beyond it we treat the
# reference as unresolved (null) and let the coach ask. Comfortably covers the race
# horizon (San Sebastian, 22 Nov 2026) from any sensible "now".
_MAX_HORIZON_DAYS = 400

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

VALID_INTENTS = frozenset({
    "training_feedback",  # change an upcoming session (defaults to tomorrow)
    "food_log",           # logging what was eaten
    "mobility_log",       # logging mobility/recovery work
    "question",           # asking a question, not logging or changing
    "none_clear",         # ambiguous; ask for clarification
})

# TODO(refactor): parameterise user name for multi-user phase.
_SYSTEM_PROMPT = """\
You are classifying a fitness app reply from Luke Evans.
He may reply about:
- Training feedback (wants to change tomorrow's session, e.g. "swap the run for easy", "I'm wrecked, drop volume")
- Food log (reporting what he ate, e.g. "3 eggs and toast for breakfast, chicken rice for lunch")
- Mobility log (reporting recovery work, e.g. "did 20 min mobility, hips tight")
- Question (asking something, e.g. "did I hit protein today?", "what's tomorrow's pace?")
- Or any combination of the above in one reply.

For each intent you detect, return the exact text fragment from his reply that belongs to it.
Do not paraphrase. Do not invent intents. If the reply is unclear, return [{"intent": "none_clear", "text": <full reply>}].

For a training_feedback intent, also extract which day it refers to as "target_date":
- If he names a day or relative reference, copy that phrase verbatim (e.g. "Thursday",
  "Friday's session", "this weekend", "tomorrow", "tonight", or an explicit "2026-08-14").
- If no day is mentioned, use null. Do NOT guess a date; the app resolves the phrase itself.

OUTPUT JSON ONLY. Schema:
{
  "intents": [
    {"intent": "training_feedback|food_log|mobility_log|question|none_clear", "text": "<exact fragment>", "target_date": "<day phrase or null>"}
  ]
}
"""


def _resolve_target_date(raw, today: date) -> "str | None":
    """Resolve a free-text day reference to an absolute ISO date, or None.

    Handles ISO dates, weekday names (next occurrence on or after today), and common
    relative phrases ("today/tonight", "tomorrow", "this weekend"). Resolution is in
    Amsterdam time (via the supplied `today`). Never resolves to a past date, and treats
    anything beyond the horizon as unresolved.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text or text in ("null", "none"):
        return None

    resolved: "date | None" = None

    iso_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if iso_match:
        try:
            resolved = date.fromisoformat(iso_match.group(0))
        except ValueError:
            resolved = None

    if resolved is None:
        if "tomorrow" in text:
            resolved = today + timedelta(days=1)
        elif "today" in text or "tonight" in text:
            resolved = today
        elif "weekend" in text:
            # Next Saturday on or after today.
            resolved = today + timedelta(days=(5 - today.weekday()) % 7)
        else:
            for token in re.findall(r"[a-z]+", text):
                if token in _WEEKDAYS:
                    delta = (_WEEKDAYS[token] - today.weekday()) % 7
                    resolved = today + timedelta(days=delta)
                    break

    if resolved is None:
        return None
    if resolved < today or resolved > today + timedelta(days=_MAX_HORIZON_DAYS):
        return None
    return resolved.isoformat()


def classify(reply_text: str, today: "date | None" = None) -> dict:
    """Return {"intents": [{"intent": ..., "text": ..., "target_date": ...}, ...]}.

    For training_feedback intents, target_date is resolved to an absolute ISO date in
    Amsterdam time (next occurrence on or after today), or None when no day is referenced
    or the reference is unparseable/out of horizon. `today` is injectable for tests.

    Raises ValueError on bad JSON, missing/invalid intent values, or missing text.
    Raises RuntimeError on Gemini HTTP error (propagated from gemini_client).
    """
    if today is None:
        today = datetime.now(_TZ_AMSTERDAM).date()
    prompt = _SYSTEM_PROMPT + "\n\nLuke's reply:\n" + reply_text
    response = gemini_client.call_gemini(prompt)

    try:
        result = json.loads(response)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Classifier returned invalid JSON: {exc}\nText: {response[:300]}"
        ) from exc

    intents = result.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError(f"Classifier returned no intents: {result}")

    for item in intents:
        if not isinstance(item, dict):
            raise ValueError(f"Intent item not a dict: {item}")
        if item.get("intent") not in VALID_INTENTS:
            raise ValueError(f"Invalid intent: {item.get('intent')!r}")
        if not item.get("text"):
            raise ValueError(f"Intent missing text: {item}")
        if item["intent"] == "training_feedback":
            item["target_date"] = _resolve_target_date(item.get("target_date"), today)
        else:
            item["target_date"] = None

    return result
