#!/usr/bin/env python3
"""Thin wrapper around the Gemini 1.5 Flash REST API for session generation."""

import json
import os
import subprocess
import sys

_GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={{key}}"
).format(model=_GEMINI_MODEL)

_COACH_CONTEXT = """\
You are a strength-and-marathon coach for Luke Evans. Your job is to take his
feedback on tomorrow's scheduled session and return a revised session that
respects his training context.

Luke's profile:
- 32, amateur marathoner. Marathon PB 3:28:58 (Nov 2025). Target sub-3:25 for
  San Sebastián 22 Nov 2026.
- 4+ years consistent strength training, 2-3 sessions/week.
- Current strength benchmarks: Squat ~120kg e1RM, Bench ~85kg e1RM (target 96kg),
  RDL ~108kg e1RM (recent PB), OHP ~49kg e1RM. Excludes conventional deadlifts.
- Plays squash Tuesday evenings (treats as intensity).
- First baby born late May 2026 — sleep deprivation likely a major factor.

Training principles you MUST respect:
- 80/20 polarised distribution: easy must be truly easy (HR <150, 5:35-6:00/km).
  Luke's natural failure mode is running everything at moderate pace; resist this.
- Easy runs ≠ moderate runs. If Luke asks for an "easy" run, give him a slow one.
- Marathon-pace work is the highest-leverage running session (Pfitzinger principle).
- Strength: RIR 3 default. No PB attempts during marathon build.
- Compound movements before isolation. Bench is priority on push days
  (100kg trajectory).

Output tone: direct and concise. No "you've got this!" / "let's crush it!" /
motivational language. Treat Luke as a competent adult who has been training
for 8 years. Explain *why* in one sentence. Move on.\
"""

_SESSION_SCHEMA = """\
{
  "session_type": "string — descriptive name",
  "session_kind": "strength | run | rest",
  "duration_min": integer,
  "warm_up": "string (strength/run only)",
  "exercises": [{"name": "string", "sets_reps": "string", "weight": "string", "rest": "string"}],
  "run_details": {"pace": "string", "hr_target": "string", "duration": "string", "distance": "string", "effort": "string"},
  "details": "string (rest only)",
  "extras": "string (optional)",
  "short_version": "string — tired/short-on-time fallback",
  "purpose": "string — one-sentence rationale",
  "coach_note": "string — brief explanation to Luke of what changed and why"
}\
"""

_REQUIRED_KEYS = {"session_type", "session_kind", "duration_min", "short_version", "purpose", "coach_note"}
_VALID_KINDS = {"strength", "run", "rest"}


def generate_session(
    reply_text: str,
    current_session: dict,
    recent_training_summary: str,
    previous_override: dict | None = None,
) -> dict:
    """Call Gemini 1.5 Flash and return a revised session dict.

    Raises ValueError if the response is unparseable or missing required keys.
    Raises RuntimeError on HTTP error or missing API key.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY env var not set.")

    prev_section = ""
    if previous_override:
        prev_section = (
            "\nA previous override was already applied for this date:\n"
            + json.dumps(previous_override, indent=2)
            + "\nThis is your starting point — Luke is refining further.\n"
        )

    prompt = "\n\n".join([
        _COACH_CONTEXT,
        "Current scheduled session (for the date you are revising):\n" + json.dumps(current_session, indent=2) + prev_section,
        "Recent training (last 14 days):\n" + recent_training_summary,
        "Luke's feedback message (his reply):\n" + reply_text,
        (
            "Output a revised session as JSON matching this exact schema:\n"
            + _SESSION_SCHEMA
            + "\n\nOnly output the JSON. No surrounding text.\n\n"
            "If Luke's feedback is unclear, dangerous, or contradicts safe progression "
            "(e.g., excessive volume jump, injury-risky combo), return the ORIGINAL session "
            "unchanged and explain in coach_note. Do not adjust beyond reasonable bounds."
        ),
    ])

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }

    url = GEMINI_URL.format(key=api_key)
    result = subprocess.run(
        [
            "curl", "-s", "-w", "\nHTTP_STATUS:%{http_code}\n",
            "-X", "POST", url,
            "-H", "Content-Type: application/json",
            "--data-binary", json.dumps(payload),
        ],
        capture_output=True,
        text=True,
    )

    if result.stderr:
        print("GEMINI STDERR:", result.stderr, file=sys.stderr)

    output = result.stdout
    if "HTTP_STATUS:200" not in output:
        status = next((ln for ln in output.splitlines() if ln.startswith("HTTP_STATUS:")), "unknown")
        raise RuntimeError(f"Gemini API returned {status}.\nResponse body: {output[:500]}")

    body = output.rsplit("\nHTTP_STATUS:", 1)[0].strip()

    try:
        response = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini outer response not valid JSON: {exc}\nBody: {body[:500]}") from exc

    try:
        session_text = response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ValueError(
            f"Unexpected Gemini response structure: {exc}\n"
            f"Response: {json.dumps(response)[:500]}"
        ) from exc

    try:
        session = json.loads(session_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini session output not valid JSON: {exc}\nText: {session_text[:500]}") from exc

    missing = _REQUIRED_KEYS - set(session.keys())
    if missing:
        raise ValueError(f"Gemini session missing required keys: {missing}")

    if session.get("session_kind") not in _VALID_KINDS:
        raise ValueError(
            f"session_kind must be one of {_VALID_KINDS}, got: {session.get('session_kind')!r}"
        )

    return session
