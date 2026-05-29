"""Strong CSV adapter — the only place that knows the Strong app's export format.

Reads a Strong workout export and emits normalised `LiftSet` records with canonical
exercise labels (lowercase, hyphens normalised, key lifts collapsed to a single label).
A future strength data source implements the same `read_lifts(path) -> list[LiftSet]`
contract and nothing downstream changes.
"""

import csv
from datetime import datetime

from .models import LiftSet

# Canonical space-separated key lifts (no hyphen variants; matched case-insensitively).
# Longer entries must win over shorter ones, so matching iterates by descending length.
_KEY_LIFTS = {
    "back squat",
    "barbell bench press",
    "romanian deadlift",
    "overhead press",
    "standing overhead press",
    "weighted pull up",
    "pull up",
}


def _match_key_lift(exercise_raw: str) -> "str | None":
    """Return the canonical key-lift label for an exercise name, or None if not a key lift.

    Normalises hyphens to spaces before matching; picks the longest (most specific) match.
    """
    norm = exercise_raw.lower().replace("-", " ")
    for key in sorted(_KEY_LIFTS, key=len, reverse=True):
        if key in norm:
            return key
    return None


def _canonical_exercise(exercise_raw: str) -> str:
    """Canonicalise an exercise name: lowercase, hyphens normalised, key lifts collapsed.

    Key lifts collapse to their canonical label (so "Pull-Up" and "Pull Up" become the
    single label "pull up"); other exercises keep their normalised name.
    """
    key = _match_key_lift(exercise_raw)
    if key is not None:
        return key
    return exercise_raw.lower().replace("-", " ").strip()


def read_lifts(path) -> "list[LiftSet]":
    """Read a Strong CSV at `path` and return normalised `LiftSet` records.

    Emits one LiftSet per parseable working set (every exercise, not just key lifts), so
    callers can count sessions and pick top sets. Rows with an unparseable date are
    skipped silently. The consumer applies its own timezone-correct cutoff window.

    Raises FileNotFoundError if `path` does not exist (callers translate to a warning).
    """
    lifts: "list[LiftSet]" = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date_str = (row.get("Date") or "")[:10]
                act_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                exercise = _canonical_exercise((row.get("Exercise Name") or "").strip())
                weight = float(row.get("Weight") or 0)
                reps = int(float(row.get("Reps") or 0))
                lifts.append(LiftSet(
                    date=act_date,
                    exercise=exercise,
                    weight_kg=weight,
                    reps=reps,
                ))
            except Exception:
                continue
    return lifts
