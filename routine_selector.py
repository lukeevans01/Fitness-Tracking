"""Deterministic strength-session selection from the routine template library.

The daily-email workflow has no Gemini key, so selection must be pure Python. Given the
six routine templates and Luke's recent Strong-CSV history, this picks the template whose
muscle emphasis least overlaps what he has trained in the last few days — directly
answering the "I did a similar one earlier in the week" problem — and renders it into the
session dict the email renderer expects.

Override-driven sessions and run/rest days are untouched; this only supplies content for
template strength days when no feedback override exists. On any error the caller falls
back to the static plan_template day.

Run the tests with:  python3 -m unittest tests.test_routine_selector
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import routine_library
from ingest import strong_csv

_ROOT = Path(__file__).parent
_STRONG_CSV = _ROOT / "data" / "strong.csv"

# Canonical muscle groups (subset of muscle_taxonomy.md used for overlap scoring).
# Each rule maps substrings in an exercise name to the primary groups it loads.
# Order matters only for readability; matching is substring-based and additive.
_MUSCLE_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("back squat", "front squat", "goblet", "split squat", "lunge", "leg press", "leg extension"),
     ("quads", "glutes")),
    (("romanian deadlift", "rdl", "deadlift", "leg curl", "good morning"),
     ("hamstrings", "glutes")),
    (("hip thrust", "glute bridge"), ("glutes",)),
    (("calf raise", "calf"), ("calves",)),
    (("bench", "chest", "dip", "fly", "flys", "push-up", "push up"), ("chest",)),
    (("overhead press", "ohp", "shoulder press", "landmine press", "lateral raise", "ohp"),
     ("shoulders",)),
    (("pull-up", "pull up", "pulldown", "row", "chin-up", "chin up", "face pull"), ("back",)),
    (("tricep", "pushdown", "skull", "close-grip", "close grip"), ("triceps",)),
    (("curl", "bicep"), ("biceps",)),
    (("pallof", "woodchop", "plank", "dead bug", "leg raise", "knee raise", "carry",
      "core", "ab "), ("core",)),
    (("kettlebell swing", "swing", "clean", "snatch"), ("glutes", "hamstrings")),
]


def classify_muscles(exercise_name: str) -> set[str]:
    """Map an exercise name to the canonical muscle groups it primarily loads.

    Substring match against _MUSCLE_RULES. Returns an empty set when nothing matches
    (e.g. an unknown accessory) so it contributes no overlap.
    """
    name = exercise_name.lower()
    groups: set[str] = set()
    for needles, muscles in _MUSCLE_RULES:
        if any(n in name for n in needles):
            groups.update(muscles)
    return groups


def template_emphasis(template: "routine_library.RoutineTemplate") -> dict[str, int]:
    """Sets-weighted muscle emphasis for a template: muscle group -> total sets."""
    emphasis: dict[str, int] = {}
    for ex in template.exercises:
        for muscle in classify_muscles(ex.name):
            emphasis[muscle] = emphasis.get(muscle, 0) + max(ex.sets, 1)
    return emphasis


def recent_muscle_load(
    today: date,
    window_days: int = 10,
    strong_path: Path | None = None,
) -> dict[str, float]:
    """Muscle group -> number of recent working sets, from the Strong CSV.

    Looks back `window_days` from `today` (exclusive of older sets). Missing/unreadable
    CSV yields an empty load (so selection still works on a cold start). Deterministic.
    """
    path = strong_path or _STRONG_CSV
    try:
        lifts = strong_csv.read_lifts(path)
    except FileNotFoundError:
        return {}
    cutoff_ordinal = today.toordinal() - window_days
    load: dict[str, float] = {}
    for lift in lifts:
        if lift.date.toordinal() <= cutoff_ordinal or lift.date > today:
            continue
        for muscle in classify_muscles(lift.exercise):
            load[muscle] = load.get(muscle, 0.0) + 1.0
    return load


def score_template(
    template: "routine_library.RoutineTemplate",
    recent_load: dict[str, float],
) -> float:
    """Overlap score: sum over the template's emphasised muscles of (sets * recent load).

    Lower is fresher (less of what was trained recently). Zero recent load -> score 0,
    so on a cold start every template ties and the stable sort decides.
    """
    emphasis = template_emphasis(template)
    return sum(sets * recent_load.get(muscle, 0.0) for muscle, sets in emphasis.items())


def select_template(
    today: date,
    templates: "list[routine_library.RoutineTemplate] | None" = None,
    window_days: int = 10,
    strong_path: Path | None = None,
) -> "routine_library.RoutineTemplate | None":
    """Pick the freshest template (lowest recent-overlap score).

    Tie-break is stable by (week, slot) via load_templates' ordering, so behaviour is
    deterministic. Returns None when no templates are available.
    """
    templates = routine_library.load_templates() if templates is None else templates
    if not templates:
        return None
    recent = recent_muscle_load(today, window_days=window_days, strong_path=strong_path)
    # min() keeps the first of equal scores; templates are already (week, slot)-sorted.
    return min(templates, key=lambda t: score_template(t, recent))


# ──────────────────────────────────────────────────────────────────────────
# Render a template into the email session dict
# ──────────────────────────────────────────────────────────────────────────

# Anchored working weights (~72% of e1RM, RIR 3) for the main barbell lifts. Mirrors the
# benchmarks in specialists/lifting.py. Everything else gets RIR-3 guidance text.
_WEIGHT_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("back squat",), "~85 kg"),
    (("front squat",), "~60 kg"),
    (("romanian deadlift", "rdl"), "~78 kg"),
    (("barbell bench", "bench press"), "~62 kg"),
    (("overhead press", "ohp"), "~35 kg"),
]

_BODYWEIGHT_NEEDLES = (
    "pull-up", "pull up", "chin-up", "chin up", "dip", "push-up", "push up",
    "plank", "dead bug", "pallof", "hanging", "leg raise", "knee raise",
    "woodchop", "glute bridge",
)


def _weight_hint(name: str) -> str:
    low = name.lower()
    for needles, hint in _WEIGHT_HINTS:
        if any(n in low for n in needles):
            return hint
    if any(n in low for n in _BODYWEIGHT_NEEDLES):
        return "Bodyweight"
    return "RIR 3"


def _rest_hint(kind: str) -> str:
    k = kind.lower()
    if k == "compound":
        return "90-120s"
    if k == "power":
        return "2 min"
    return "60s"


def _sets_reps(ex: "routine_library.RoutineExercise") -> str:
    base = f"{ex.sets} x {ex.reps}"
    return f"{base} {ex.notes}".rstrip() if ex.notes else base


def _estimate_duration_min(template: "routine_library.RoutineTemplate") -> int:
    """Rough session length: compound 3 min/set, power 2.5, else 2; +8 warm-up. Cap 75."""
    per_set = {"compound": 3.0, "power": 2.5}
    total = 8.0
    for ex in template.exercises:
        total += max(ex.sets, 1) * per_set.get(ex.kind.lower(), 2.0)
    return min(int(round(total)), 75)


def routine_to_session(template: "routine_library.RoutineTemplate") -> dict:
    """Render a RoutineTemplate into the session dict the daily email expects."""
    exercises = [
        {
            "name": ex.name,
            "sets_reps": _sets_reps(ex),
            "weight": _weight_hint(ex.name),
            "rest": _rest_hint(ex.kind),
        }
        for ex in template.exercises
    ]
    primary = template.primary
    focus = primary.name if primary else "full body"
    # short_version: keep the compounds, drop isolation/core.
    compounds = [ex.name for ex in template.exercises if ex.kind.lower() in ("compound", "power")]
    keep = compounds[:3] if compounds else [exercises[0]["name"]]
    short = "Do " + ", ".join(keep) + " for 2 sets each at RIR 3. Skip isolation and core."
    return {
        "session_type": f"Full Body Strength - {template.name} ({focus} focus)",
        "session_kind": "strength",
        "duration_min": _estimate_duration_min(template),
        "warm_up": "5-8 min easy bike or row, then 2 ramp-up sets on the first compound",
        "exercises": exercises,
        "short_version": short,
        "purpose": (
            f"{template.name}, chosen to avoid repeating the muscle groups you trained "
            "in the last few days. Compound first, RIR 3 throughout."
        ),
    }


def select_session(
    today: date,
    templates: "list[routine_library.RoutineTemplate] | None" = None,
    window_days: int = 10,
    strong_path: Path | None = None,
) -> "dict | None":
    """Select the freshest template and render it to a session dict.

    Returns None when no templates are available so the caller can fall back to the
    static plan_template strength day. Never raises on missing data.
    """
    template = select_template(
        today, templates=templates, window_days=window_days, strong_path=strong_path
    )
    if template is None:
        return None
    return routine_to_session(template)
