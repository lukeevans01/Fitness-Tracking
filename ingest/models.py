"""Normalised activity and lift records — the Phase 4 ingestion boundary.

Every data source (Strava CSV today; a Strava or Apple Health API tomorrow) emits these
records, so the coaching layer never sees source-specific formats. Keep these minimal and
stable: they are the contract adapters implement against.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Activity:
    """A single cardio activity (run, ride, swim, ...)."""

    date: date
    kind: str                       # "run" | "ride" | "swim" | "other"
    distance_km: float
    moving_s: float
    avg_hr: "float | None"
    pace_min_km: "float | None"     # derived from distance/time if not supplied


@dataclass(frozen=True)
class LiftSet:
    """A single working set from a strength session."""

    date: date
    exercise: str                   # canonical, lowercase, hyphens normalised
    weight_kg: float
    reps: int
