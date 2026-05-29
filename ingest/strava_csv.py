"""Strava CSV adapter — the only place that knows Strava's column quirks.

Reads a Strava activity export and emits normalised `Activity` records. A future
`ingest/strava_api.py` or `ingest/apple_health.py` implements the same
`read_activities(path, today=None) -> list[Activity]` contract, so nothing downstream
changes when the data source does.

Column-index resolution and distance normalisation are the hardened logic from pack 02.
"""

import csv
from datetime import date, datetime

from .models import Activity


def _parse_strava_date(value: str) -> "date | None":
    """Parse a Strava activity date — ISO first, then the 'May 24, 2026, 5:31:14 AM' export format."""
    value = value.strip()
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_duration_seconds(value: str) -> float:
    """Parse 'HH:MM:SS' or a raw seconds string to float seconds."""
    value = value.strip()
    if ":" in value:
        parts = value.split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            pass
    try:
        return float(value)
    except ValueError:
        return 0.0


def _strava_column_index(header: list) -> dict:
    """Resolve Strava column names to explicit indices; first occurrence wins for duplicates."""
    idx: dict = {}
    for i, name in enumerate(header):
        name = name.strip()
        if name and name not in idx:
            idx[name] = i
    return idx


def _normalise_distance_km(raw: float) -> "float | None":
    """Convert a raw Strava distance value to km, or return None if implausible.

    Strava exports distance either in km (summary column) or metres (stream column); a
    value at or above 100 is treated as metres. Returns None for non-positive or absurd
    distances so the caller can decide whether to keep the activity.
    """
    if raw <= 0:
        return None
    km = raw if raw < 100 else raw / 1000.0
    if not (0 < km < 100):
        return None
    return round(km, 2)


def _classify_kind(raw_type: str) -> str:
    """Map a raw Strava activity type to a normalised kind."""
    t = raw_type.strip().lower()
    if t in ("run", "running"):
        return "run"
    if "swim" in t:
        return "swim"
    if "ride" in t or "cycl" in t or "bike" in t:
        return "ride"
    return "other"


def read_activities(path, today: "date | None" = None) -> "list[Activity]":
    """Read a Strava CSV at `path` and return normalised `Activity` records.

    Emits one Activity per parseable row (any kind). Rows that cannot be parsed
    (bad date, garbage distance/HR) are skipped silently — the consumer applies its own
    timezone-correct cutoff window, so `today` is accepted for contract symmetry but the
    full set of parseable activities is returned regardless. Distance that is missing,
    empty, or implausible yields distance_km=0.0 (and pace None); the consumer decides
    whether a zero-distance activity counts.

    Raises FileNotFoundError if `path` does not exist (callers translate to a warning).
    """
    activities: "list[Activity]" = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return activities
        idx = _strava_column_index(header)
        type_col = idx.get("Activity Type")
        date_col = idx.get("Activity Date") if "Activity Date" in idx else idx.get("Start Date")
        dist_col = idx.get("Distance")
        time_col = idx.get("Moving Time")
        hr_col = idx.get("Average Heart Rate")
        for row in reader:
            try:
                if type_col is None or type_col >= len(row):
                    continue
                kind = _classify_kind(row[type_col])
                if date_col is None or date_col >= len(row):
                    continue
                act_date = _parse_strava_date(row[date_col])
                if act_date is None:
                    continue
                distance_km = 0.0
                if dist_col is not None and dist_col < len(row):
                    raw_str = row[dist_col].strip()
                    if raw_str:
                        norm = _normalise_distance_km(float(raw_str))
                        if norm is not None:
                            distance_km = norm
                moving_s = 0.0
                if time_col is not None and time_col < len(row):
                    moving_s = _parse_duration_seconds(row[time_col])
                avg_hr = None
                if hr_col is not None and hr_col < len(row):
                    hr_raw = row[hr_col].strip()
                    if hr_raw:
                        avg_hr = float(hr_raw)
                pace_min_km = (moving_s / 60) / distance_km if distance_km > 0 else None
                activities.append(Activity(
                    date=act_date,
                    kind=kind,
                    distance_km=distance_km,
                    moving_s=moving_s,
                    avg_hr=avg_hr,
                    pace_min_km=pace_min_km,
                ))
            except Exception:
                continue
    return activities
