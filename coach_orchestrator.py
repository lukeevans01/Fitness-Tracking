#!/usr/bin/env python3
"""
Coach orchestrator — routes feedback to the appropriate specialist and calls Gemini.

Use this instead of gemini_client.generate_session() when you want domain-specific
coaching context (running vs. lifting vs. mobility). The existing gemini_client module
remains unchanged and is still used by process_replies.py until that is migrated here.

Usage:
    from coach_orchestrator import generate_session

    session = generate_session(
        domain="run",          # "run" | "strength" | "rest" | "nutrition"
        reply_text="...",
        current_session={...},
        training_summary="...",
        previous_override=None,
    )
"""

import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gemini_client
from specialists import lifting, mobility, nutrition, nutrition_lookup, running

_ROOT = Path(__file__).parent
_TZ = ZoneInfo("Europe/Amsterdam")
_RACE_DATE = date(2026, 11, 22)
_TAPER_WINDOW_DAYS = 28
_RACE_WEEK_DAYS = 7

# TODO(refactor): bakes in Luke-specific content. Parameterise per-user in multi-user refactor.
_SHARED_PROFILE = """\
Luke Evans — 32, amateur marathoner. Marathon PB 3:28:58 (Nice-Cannes, Nov 2025).
Target: sub-3:25 at San Sebastián marathon, 22 Nov 2026.
4+ years consistent strength training. Squash Tuesday evenings (treats as intensity).
First baby born late May 2026 — sleep deprivation is a real, ongoing factor.

Hard rules that apply to every session, no exceptions:
- Sleep <6h → drop to short_version or skip entirely.
- No PB attempts in training.
- If Luke says he's wrecked, believe him.

Output tone: direct and concise. No motivational language. Treat Luke as a competent
adult with 8 years of training experience. State the change and the reason. Move on.\
"""

_SESSION_SCHEMA = """\
{
  "session_type": "string — descriptive name",
  "session_kind": "strength | run | rest",
  "duration_min": integer,
  "warm_up": "string (strength/run only, omit for rest)",
  "exercises": [{"name": "string", "sets_reps": "string", "weight": "string", "rest": "string"}],
  "run_details": {"pace": "string", "hr_target": "string", "duration": "string", "distance": "string", "effort": "string"},
  "details": "string (rest/mobility sessions only)",
  "extras": "string (optional)",
  "short_version": "string — what to do if tired or short on time",
  "purpose": "string — one sentence",
  "coach_note": "string — what changed and why, addressed to Luke"
}\
"""

_REQUIRED_KEYS = {"session_type", "session_kind", "duration_min", "short_version", "purpose", "coach_note"}
_VALID_KINDS = {"strength", "run", "rest"}

# Maps session_kind → specialist module. Use infer_domain() to derive from a session dict.
_DOMAIN_MAP = {
    "run": running,
    "strength": lifting,
    "rest": mobility,
    "nutrition": nutrition,  # not a session_kind — triggered explicitly by future nutrition flow
}


# ──────────────────────────────────────────────────────────────────────────
# Taper detection
# ──────────────────────────────────────────────────────────────────────────

def _today() -> date:
    return datetime.now(_TZ).date()


def days_to_race(today: date | None = None) -> int:
    """Days remaining until race day. Negative after race day."""
    return (_RACE_DATE - (today or _today())).days


def is_taper_active(today: date | None = None) -> bool:
    """True when within _TAPER_WINDOW_DAYS of race day (and race hasn't passed)."""
    d = days_to_race(today)
    return 0 <= d <= _TAPER_WINDOW_DAYS


def _taper_prompt_block(today: date) -> str:
    d = days_to_race(today)
    race_week = d <= _RACE_WEEK_DAYS
    block = (
        f"TAPER IS ACTIVE — RACE DAY IS IN {d} DAYS (San Sebastián, 22 Nov 2026).\n"
        "These rules are PRESCRIPTIVE. They override Luke's preferences and cannot be negotiated:\n"
        "- No volume increases. Cap sessions at 70% of standard duration/sets.\n"
        "- Strength: RIR 4 (not 3). 2-3 sets per compound. Skip accessories.\n"
        "- Running: easy runs only (HR <150, 5:35-6:00/km). No new distances.\n"
        "  Exception: one short marathon-pace segment (15-20 min) per week is permitted.\n"
        "- If Luke asks to train harder: reduce instead. Taper resistance is expected; hold the line.\n"
        "- coach_note MUST acknowledge the taper and state the specific reduction applied."
    )
    if race_week:
        block += (
            "\n- RACE WEEK (≤7 days to go): easy runs 20-30 min max. No strength at all. "
            "Full rest day immediately before race day. Prioritise sleep and nutrition."
        )
    return block


def sync_taper_state(today: date | None = None) -> None:
    """Update adaptation_state.md taper fields if status has changed. Call once per run."""
    path = _ROOT / "adaptation_state.md"
    if not path.exists():
        return
    t = today or _today()
    active = is_taper_active(t)
    content = path.read_text()
    new_flag = "true" if active else "false"
    content = re.sub(r"(?m)^taper_active: \S+", f"taper_active: {new_flag}", content)
    if active:
        # Set start date only if still null (preserve the first-detection date)
        content = re.sub(r"(?m)^taper_start_date: null", f"taper_start_date: {t.isoformat()}", content)
    path.write_text(content)


def infer_domain(session_kind: str) -> str:
    """Derive domain string from a session's session_kind field."""
    mapping = {"run": "run", "strength": "strength", "rest": "rest"}
    return mapping.get(session_kind, "strength")


def generate_session(
    domain: str,
    reply_text: str,
    current_session: dict,
    training_summary: str,
    previous_override: dict | None = None,
    week_context: str = "",
) -> dict:
    """Route feedback to the right specialist, call Gemini, validate, and return a session dict.

    domain: "run" | "strength" | "rest" — use infer_domain(session["session_kind"]) if unsure.
    Raises ValueError on bad JSON or missing required keys.
    Raises RuntimeError on Gemini HTTP error or missing API key.
    """
    today = _today()
    specialist = _DOMAIN_MAP.get(domain, lifting)
    prompt = _build_prompt(
        specialist, domain, reply_text, current_session,
        training_summary, previous_override, today, week_context,
    )
    session_text = gemini_client.call_gemini(prompt)

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


_WEEKLY_SUMMARY_SCHEMA = """\
{
  "week_review": "2-3 sentences: what was done last week, any notable patterns or concerns",
  "option_a": {
    "label": "Continue as planned",
    "sessions": "One line per day, e.g. Mon 01 Jun: Strength A3 (50 min)\\nTue 02 Jun: ...",
    "rationale": "One sentence"
  },
  "option_b": {
    "label": "Short descriptive label",
    "sessions": "One line per day",
    "rationale": "One sentence"
  },
  "option_c": {
    "label": "Short descriptive label",
    "sessions": "One line per day",
    "rationale": "One sentence"
  },
  "recommendation": "A or B or C",
  "recommendation_reason": "One sentence",
  "coach_note": "Any additional context, or empty string"
}\
"""

_WEEKLY_SUMMARY_REQUIRED = frozenset({
    "week_review", "option_a", "option_b", "option_c",
    "recommendation", "recommendation_reason",
})
_OPTION_REQUIRED = frozenset({"label", "sessions", "rationale"})

_WEEKLY_COACH_CONTEXT = """\
You are reviewing Luke Evans's recent training and proposing three options for the coming week.
Apply your full knowledge of his profile: sub-3:25 target at San Sebastián (22 Nov 2026),
polarised 80/20 training distribution, RIR 3 strength default, squash Tuesday evenings,
sleep deprivation as an ongoing factor from late May 2026.

Produce three genuinely distinct options — not trivial variants:
- Option A: The standard plan as scheduled. Use the provided "standard week" exactly. Do not modify it.
- Option B: An evidence-based adjustment based on last week's actual load. If Luke did
  more than planned: add recovery. If he did less: keep volume stable. If he nailed it: progress
  one variable (distance, weight, or add a short quality segment).
- Option C: An alternative focus — e.g., if fatigue is building: recovery week with easy runs only;
  if strength has slipped: extra lifting; if marathon date pressure is rising: marathon-pace work.

Recommendation: pick the option that best serves sub-3:25 given the data. If training data
is sparse (no CSV loaded), recommend A with a coach_note explaining the data gap.

Tone: direct, specific, no fluff. One line per day in sessions fields. Name the session type
and duration. State specifically what changes between options.\
"""


def generate_weekly_summary(
    training_summary: str,
    standard_week: str,
    nutrition_summary: str = "",
) -> dict:
    """Generate a weekly review with three options for the coming week.

    standard_week: compact day-by-day string computed from plan_template.json.
    nutrition_summary: optional human-readable summary of last week's nutrition logs;
        passed into the prompt so options can be nutrition-aware.
    Returns a dict with week_review, option_a/b/c, recommendation, recommendation_reason, coach_note.
    Raises ValueError on bad JSON or missing required keys.
    Raises RuntimeError on Gemini HTTP error.
    """
    today = _today()
    parts = [_SHARED_PROFILE, _WEEKLY_COACH_CONTEXT]
    if is_taper_active(today):
        parts.append(_taper_prompt_block(today))
    parts += [
        "Standard week (use this exactly for Option A):\n" + standard_week,
        "Recent training (last 14 days):\n" + training_summary,
    ]
    if nutrition_summary:
        parts.append("Recent nutrition (last 7 days):\n" + nutrition_summary)
    parts.append(
        "Output the weekly review as JSON matching this exact schema:\n"
        + _WEEKLY_SUMMARY_SCHEMA
        + "\n\nOnly output the JSON. No surrounding text."
    )
    prompt = "\n\n".join(parts)

    response_text = gemini_client.call_gemini(prompt)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini weekly summary not valid JSON: {exc}\nText: {response_text[:500]}") from exc

    missing = _WEEKLY_SUMMARY_REQUIRED - set(result.keys())
    if missing:
        raise ValueError(f"Weekly summary missing required keys: {missing}")

    for key in ("option_a", "option_b", "option_c"):
        opt_missing = _OPTION_REQUIRED - set(result.get(key, {}).keys())
        if opt_missing:
            raise ValueError(f"{key} missing keys: {opt_missing}")

    if result.get("recommendation") not in ("A", "B", "C"):
        raise ValueError(f"recommendation must be A, B, or C, got: {result.get('recommendation')!r}")

    return result


def _build_prompt(
    specialist,
    domain: str,
    reply_text: str,
    current_session: dict,
    training_summary: str,
    previous_override: dict | None,
    today: date,
    week_context: str,
) -> str:
    parts = [
        _SHARED_PROFILE,
        specialist.system_context(),
    ]
    if is_taper_active(today):
        parts.append(_taper_prompt_block(today))
    if week_context:
        parts.append(
            "Luke's chosen week plan (context for this session):\n" + week_context
        )
    parts += [
        "Current scheduled session:\n" + json.dumps(current_session, indent=2),
    ]
    if previous_override:
        parts.append(
            "A previous override was already applied for this date:\n"
            + json.dumps(previous_override, indent=2)
            + "\nThis is your starting point — Luke is refining further."
        )
    # Nutrition replies should route to generate_food_log_response(), not here — this branch
    # is retained defensively in case a caller still passes domain="nutrition".
    if domain == "nutrition":
        food_data = nutrition_lookup.enrich_prompt_with_food_data(reply_text)
        if food_data:
            parts.append(food_data)
    parts.extend([
        "Recent training (last 14 days):\n" + training_summary,
        "Luke's feedback:\n" + reply_text,
        (
            "Output a revised session as JSON matching this exact schema:\n"
            + _SESSION_SCHEMA
            + "\n\nOnly output the JSON. No surrounding text.\n\n"
            "If Luke's feedback is unclear, unsafe, or contradicts safe progression, "
            "return the ORIGINAL session unchanged and explain in coach_note."
        ),
    ])
    return "\n\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────
# Nutrition — food log parsing and Q&A
# ──────────────────────────────────────────────────────────────────────────

_FOOD_LOG_SCHEMA = """\
{
  "items": [
    {
      "name": "string — e.g. 'Banana', 'Chicken breast', 'Chicken curry'",
      "quantity": "string — human-readable, e.g. '3 large', '1 cup', '200g'",
      "quantity_g": number — your best estimate of portion weight in grams,
      "kcal": number,
      "protein_g": number,
      "carbs_g": number,
      "fat_g": number,
      "confidence": "high | medium | low",
      "source": "off | needs_lookup | gemini",
      "meal": "breakfast | lunch | dinner | snack | unspecified"
    }
  ],
  "coach_note": "string — optional one-line note from the nutrition coach; empty string if nothing useful"
}\
"""

_FOOD_LOG_REQUIRED = frozenset({"items", "coach_note"})
_FOOD_ITEM_REQUIRED = frozenset({
    "name", "quantity", "quantity_g",
    "kcal", "protein_g", "carbs_g", "fat_g",
    "confidence", "source", "meal",
})


# TODO(refactor): per-user targets and prompt content. Hardcoded to Luke for Phase 2.
def generate_food_log_response(
    reply_text: str,
    today_so_far: dict,
    targets: dict,
    recent_pattern: str = "",
) -> dict:
    """Parse a food-log reply into structured items + brief coach note.

    today_so_far: dict with keys protein_g, carbs_g, fat_g, kcal for what's already been logged today.
    targets: DAILY_TARGETS dict from nutrition_logger.
    recent_pattern: optional one-line pattern flag from weekly_summary (e.g. "protein <100g for 3 days").

    Returns {"items": [...], "coach_note": "..."}.
    Raises ValueError on bad JSON or missing required keys.
    Raises RuntimeError on Gemini HTTP error.
    """
    parts = [
        _SHARED_PROFILE,
        nutrition.system_context(),
        (
            "Today's running totals BEFORE this log entry:\n"
            f"  Protein: {today_so_far.get('protein_g', 0):.1f}g of {targets['protein_g']}g\n"
            f"  Carbs:   {today_so_far.get('carbs_g', 0):.1f}g of {targets['carbs_g']}g\n"
            f"  Fat:     {today_so_far.get('fat_g', 0):.1f}g of {targets['fat_g']}g\n"
            f"  Calories: {today_so_far.get('kcal', 0):.0f} of {targets['kcal']}"
        ),
    ]
    if recent_pattern:
        parts.append("Recent pattern context: " + recent_pattern)
    parts += [
        "Luke's food log reply:\n" + reply_text,
        (
            "Parse this reply into discrete food items with macro estimates.\n\n"
            "Rules:\n"
            "- Infer the meal from context (breakfast/lunch/dinner/snack), else 'unspecified'.\n"
            "- ALWAYS include quantity_g (grams) — your best estimate of portion weight. This is "
            "used to scale data sources, so be realistic (1 medium banana ≈ 120g, 1 large egg ≈ 50g).\n"
            "- For each item, choose source:\n"
            "    * 'needs_lookup' for common whole/branded foods where Open Food Facts would have "
            "authoritative per-100g data (banana, eggs, oats, chicken, yogurt, bread, rice, pasta, milk). "
            "Still provide your own kcal/protein/carbs/fat as a fallback in case lookup misses.\n"
            "    * 'gemini' for composite/cooked meals, restaurant-style items, or anything OFF won't "
            "reliably know (chicken curry, lasagne, pad thai, smoothie with multiple ingredients).\n"
            "- Confidence: 'high' for items with explicit quantity; 'medium' if quantity is implied; "
            "'low' for vague/composite meals where the estimate could easily be ±30%.\n"
            "- coach_note: optional, single line. Use only when there's something genuinely useful "
            "to flag (e.g. 'You're under-fueled for tomorrow's long run — eat more carbs tonight.'). "
            "Empty string otherwise.\n\n"
            "Output JSON ONLY matching this schema:\n"
            + _FOOD_LOG_SCHEMA
            + "\n\nNo surrounding text."
        ),
    ]
    prompt = "\n\n".join(parts)
    response_text = gemini_client.call_gemini(prompt)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Food log response not valid JSON: {exc}\nText: {response_text[:500]}"
        ) from exc

    missing = _FOOD_LOG_REQUIRED - set(result.keys())
    if missing:
        raise ValueError(f"Food log response missing required keys: {missing}")
    if not isinstance(result["items"], list):
        raise ValueError("Food log 'items' must be a list")
    for idx, item in enumerate(result["items"]):
        if not isinstance(item, dict):
            raise ValueError(f"Food log item {idx} not a dict: {item}")
        item_missing = _FOOD_ITEM_REQUIRED - set(item.keys())
        if item_missing:
            raise ValueError(f"Food log item {idx} missing keys: {item_missing}")
    return result


# TODO(refactor): per-user prompt and targets. Hardcoded to Luke for Phase 2.
def answer_nutrition_question(
    question: str,
    day_log,
    targets: dict,
    weekly_summary: dict | None = None,
) -> str:
    """Answer a nutrition question using today's log + targets + optional weekly context.

    day_log: nutrition_logger.DayLog | None — duck-typed to avoid a circular import.
        Expected to expose .items (list of FoodItem) and .log_date (date) when present.
    Returns a short prose answer (1-3 sentences typical).
    Raises ValueError if Gemini's wrapped response is malformed.
    Raises RuntimeError on Gemini HTTP error.
    """
    parts = [_SHARED_PROFILE, nutrition.system_context()]

    if day_log is not None and getattr(day_log, "items", None):
        totals = {
            "protein_g": sum(i.protein_g for i in day_log.items),
            "carbs_g": sum(i.carbs_g for i in day_log.items),
            "fat_g": sum(i.fat_g for i in day_log.items),
            "kcal": sum(i.kcal for i in day_log.items),
        }
        items_summary = "\n".join(
            f"  - {i.meal}: {i.name} ({i.quantity}) — "
            f"{i.kcal:.0f} kcal, {i.protein_g:.1f}g P, {i.carbs_g:.1f}g C, {i.fat_g:.1f}g F"
            for i in day_log.items
        )
        parts.append(
            f"Today's log ({day_log.log_date.isoformat()}):\n{items_summary}\n\n"
            f"Today's totals: {totals['kcal']:.0f} kcal, {totals['protein_g']:.1f}g protein, "
            f"{totals['carbs_g']:.1f}g carbs, {totals['fat_g']:.1f}g fat"
        )
    else:
        parts.append("Today's log: (no food logged yet today)")

    parts.append(
        f"Daily targets: {targets['kcal']} kcal, {targets['protein_g']}g protein, "
        f"{targets['carbs_g']}g carbs, {targets['fat_g']}g fat"
    )

    if weekly_summary and weekly_summary.get("days_logged", 0) > 0:
        parts.append(
            f"This week ({weekly_summary['days_logged']}/7 days logged): "
            f"avg {weekly_summary['avg_protein_g']:.0f}g protein, "
            f"{weekly_summary['avg_kcal']:.0f} kcal. "
            f"Protein target hit {weekly_summary['protein_target_hits']}/"
            f"{weekly_summary['days_logged']} days."
        )

    parts += [
        "Luke's question:\n" + question,
        (
            "Answer concisely (1-3 sentences max). Use the actual numbers from his log where "
            "relevant. If suggesting food to close a macro gap, give one specific option with "
            "a quantity (e.g. 'a 200g Greek yogurt + banana would add ~25g protein, ~35g carbs'). "
            "No motivational language.\n\n"
            'Output JSON ONLY: {"answer": "<your answer>"}\n'
            "No surrounding text."
        ),
    ]
    prompt = "\n\n".join(parts)
    response_text = gemini_client.call_gemini(prompt)

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Nutrition Q&A response not valid JSON: {exc}\nText: {response_text[:300]}"
        ) from exc
    answer = parsed.get("answer", "").strip()
    if not answer:
        raise ValueError(f"Nutrition Q&A response missing 'answer': {parsed}")
    return answer
