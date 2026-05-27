#!/usr/bin/env python3
"""Classify a Luke reply into one or more intents and split text accordingly.

Single entry point: classify(reply_text) -> {"intents": [{"intent": ..., "text": ...}, ...]}

Used by process_replies.py to dispatch a freeform reply to the right handler
(training feedback, food log, mobility log, question, or clarify).
"""

import json

import gemini_client

VALID_INTENTS = frozenset({
    "training_feedback",  # change tomorrow's session
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

OUTPUT JSON ONLY. Schema:
{
  "intents": [
    {"intent": "training_feedback|food_log|mobility_log|question|none_clear", "text": "<exact fragment>"}
  ]
}
"""


def classify(reply_text: str) -> dict:
    """Return {"intents": [{"intent": ..., "text": ...}, ...]}.

    Raises ValueError on bad JSON, missing/invalid intent values, or missing text.
    Raises RuntimeError on Gemini HTTP error (propagated from gemini_client).
    """
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

    return result
