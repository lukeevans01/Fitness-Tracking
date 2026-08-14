#!/usr/bin/env python3
"""Build a compact training summary for the Gemini prompt.

This module works on normalised records (`ingest.Activity` / `ingest.LiftSet`) read
through the source registry in `ingest`; it no longer knows any CSV column names or file
formats. The active source defaults to the Strava/Strong CSV adapters at the `data/`
paths below, but a future API source can register under the same interface with no change
here. See `ingest/` for the adapter contract.
"""

import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ingest
# Re-exported low-level helpers (kept importable for the Pack 09 breakdown/tonnage/squash
# readers below and for weekly_load, which imports _match_key_lift from this module).
from ingest.strava_csv import (
    _normalise_distance_km,
    _parse_duration_seconds,
    _parse_strava_date,
    _strava_column_index,
)
from ingest.strong_csv import _KEY_LIFTS, _match_key_lift

DATA_DIR = Path(__file__).parent / "data"
STRAVA_CSV = DATA_DIR / "strava.csv"
STRONG_CSV = DATA_DIR / "strong.csv"

# Active ingestion source (registry key in `ingest`). Selectable for a future API source.
ACTIVE_SOURCE = "csv"

_TZ_AMS = ZoneInfo("Europe/Amsterdam")

# min/km pace boundaries for bucket classification
_EASY_MIN_PACE = 6.0      # >= 6:00/km = easy
_MODERATE_MIN_PACE = 5.1  # 5:06-5:59/km = moderate; below = quality/MP+


def _read_activities(today: "date | None") -> list:
    """Read normalised Activity records from the active source at STRAVA_CSV."""
    return ingest.get_reader("activities", ACTIVE_SOURCE)(STRAVA_CSV, today=today)


def _read_lifts() -> list:
    """Read normalised LiftSet records from the active source at STRONG_CSV."""
    return ingest.get_reader("lifts", ACTIVE_SOURCE)(STRONG_CSV)


def build_stats(days: int = 7, today: "date | None" = None) -> dict:
    """Return structured training stats for the last `days` days.

    Keys: run_sessions, run_km_total, strength_sessions, warnings.
    Used to update adaptation_state.md weekly counters. Consumes normalised records via
    the ingest adapters; the timezone-correct cutoff is applied here.
    """
    if today is None:
        today = datetime.now(_TZ_AMS).date()
    cutoff = today - timedelta(days=days)
    warnings: list = []
    run_sessions = 0
    run_km_total = 0.0
    strength_sessions = 0

    if STRAVA_CSV.exists():
        try:
            runs = [
                a for a in _read_activities(today)
                if a.kind == "run" and cutoff < a.date <= today
            ]
            run_sessions = len(runs)
            run_km_total = sum(a.distance_km for a in runs if a.distance_km > 0)
        except Exception as exc:
            warnings.append(f"Could not parse strava.csv: {exc}")
    else:
        warnings.append("strava.csv not found in data/")

    if STRONG_CSV.exists():
        try:
            strength_sessions = len({
                lift.date for lift in _read_lifts() if cutoff < lift.date <= today
            })
        except Exception as exc:
            warnings.append(f"Could not parse strong.csv: {exc}")
    else:
        warnings.append("strong.csv not found in data/")

    for w in warnings:
        print(f"[warning] {w}", file=sys.stderr)

    return {
        "run_sessions": run_sessions,
        "run_km_total": round(run_km_total, 1),
        "strength_sessions": strength_sessions,
        "warnings": warnings,
    }


def build_strength_tonnage(days: int = 7, today: "date | None" = None) -> float:
    """Total tonnage (sum of weight_kg * reps) across key lifts in the last `days` days.

    Deterministic; key-lift only so accessory noise does not dominate. Returns 0.0 if no
    strong.csv or no matching sets. Used by weekly_load.WeeklyLoad.
    """
    if today is None:
        today = datetime.now(_TZ_AMS).date()
    cutoff = today - timedelta(days=days)
    if not STRONG_CSV.exists():
        return 0.0
    tonnage = 0.0
    try:
        with open(STRONG_CSV, newline="", encoding="utf-8-sig") as f:
            reader_s = csv.DictReader(f)
            for row in reader_s:
                try:
                    date_str = (row.get("Date") or "")[:10]
                    act_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if not (cutoff < act_date <= today):
                        continue
                    if _match_key_lift((row.get("Exercise Name") or "").strip()) is None:
                        continue
                    weight = float(row.get("Weight") or 0)
                    reps = int(float(row.get("Reps") or 0))
                    if weight > 0 and reps > 0:
                        tonnage += weight * reps
                except Exception:
                    continue
    except Exception:
        return 0.0
    return round(tonnage, 1)


def build_squash_sessions(days: int = 7, today: "date | None" = None) -> int:
    """Count Strava activities of type 'squash' in the last `days` days.

    Squash is logged as a generic activity type in Strava; this lets WeeklyLoad reflect
    Tuesday squash without inflating run counts. Returns 0 if no strava.csv.
    """
    if today is None:
        today = datetime.now(_TZ_AMS).date()
    cutoff = today - timedelta(days=days)
    if not STRAVA_CSV.exists():
        return 0
    count = 0
    try:
        with open(STRAVA_CSV, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return 0
            idx = _strava_column_index(header)
            type_col = idx.get("Activity Type")
            date_col = idx.get("Activity Date") if "Activity Date" in idx else idx.get("Start Date")
            if type_col is None:
                return 0
            for row in reader:
                try:
                    if type_col >= len(row):
                        continue
                    if "squash" not in row[type_col].strip().lower():
                        continue
                    if date_col is None or date_col >= len(row):
                        continue
                    act_date = _parse_strava_date(row[date_col])
                    if act_date is None or not (cutoff < act_date <= today):
                        continue
                    count += 1
                except Exception:
                    continue
    except Exception:
        return 0
    return count


def build_summary(days: int = 14, today: "date | None" = None) -> str:
    if today is None:
        today = datetime.now(_TZ_AMS).date()
    cutoff = today - timedelta(days=days)
    lines = []

    # ── Strava runs ──────────────────────────────────────────────────────
    if STRAVA_CSV.exists():
        runs = []
        try:
            runs = [
                a for a in _read_activities(today)
                if a.kind == "run" and cutoff < a.date <= today and a.distance_km > 0
            ]
        except Exception as exc:
            lines.append(f"[warning: could not parse strava.csv: {exc}]")

        if runs:
            total_km = sum(r.distance_km for r in runs)
            easy = sum(1 for r in runs if r.pace_min_km and r.pace_min_km >= _EASY_MIN_PACE)
            moderate = sum(
                1 for r in runs
                if r.pace_min_km and _MODERATE_MIN_PACE <= r.pace_min_km < _EASY_MIN_PACE
            )
            quality = sum(1 for r in runs if r.pace_min_km and r.pace_min_km < _MODERATE_MIN_PACE)
            longest = max(runs, key=lambda r: r.distance_km)
            lines.append(f"RUNS (last {days} days): {len(runs)} runs, {total_km:.1f} km total")
            lines.append(
                f"  Pace distribution -- easy (>=6:00/km): {easy}, "
                f"moderate (5:06-5:59/km): {moderate}, quality (<5:06/km): {quality}"
            )
            pace_note = f" at {longest.pace_min_km:.2f} min/km" if longest.pace_min_km else ""
            lines.append(
                f"  Longest run: {longest.distance_km} km on {longest.date}{pace_note}"
            )
            hr_runs = [r for r in runs if r.avg_hr]
            if hr_runs:
                avg_hr = sum(r.avg_hr for r in hr_runs) / len(hr_runs)
                lines.append(f"  Avg HR across runs with data: {avg_hr:.0f} bpm")
        else:
            lines.append(f"RUNS (last {days} days): none recorded in this period.")
    else:
        lines.append("RUNS: no strava.csv found in data/ -- upload via Sunday reminder.")

    lines.append("")

    # ── Strong lifts ──────────────────────────────────────────────────────
    if STRONG_CSV.exists():
        in_window = []
        try:
            in_window = [lift for lift in _read_lifts() if cutoff < lift.date <= today]
        except Exception as exc:
            lines.append(f"[warning: could not parse strong.csv: {exc}]")

        session_dates = {lift.date for lift in in_window}
        if session_dates:
            lines.append(f"STRENGTH (last {days} days): {len(session_dates)} session(s)")
            # Heaviest working set per canonical key lift across the period; first
            # occurrence wins on a tie, mirroring the prior strictly-greater comparison.
            top_sets: dict = {}
            for lift in in_window:
                if lift.exercise not in _KEY_LIFTS:
                    continue
                current = top_sets.get(lift.exercise)
                if current is None or lift.weight_kg > current[0]:
                    top_sets[lift.exercise] = (lift.weight_kg, lift.reps)
            if top_sets:
                lines.append("  Top working sets (heaviest this period):")
                for lift_name in sorted(top_sets):
                    weight, reps = top_sets[lift_name]
                    lines.append(f"    {lift_name.title()}: {weight} kg x {reps}")
        else:
            lines.append(f"STRENGTH (last {days} days): none recorded in this period.")
    else:
        lines.append("STRENGTH: no strong.csv found in data/ -- upload via Sunday reminder.")

    return "\n".join(lines)


def build_daily_breakdown(days: int = 7, today: "date | None" = None) -> dict:
    """Return per-day run and strength data for the last `days` days.

    Returns:
        {
          "runs_by_date":     {date: {"distance_km": float, "pace_min_km": float|None, "hr": float|None}},
          "strength_by_date": {date: [(exercise_name, weight_kg, reps), ...]},
        }
    Each strength entry is the top set (heaviest) per exercise for that session, sorted by name.
    """
    if today is None:
        today = datetime.now(_TZ_AMS).date()
    cutoff = today - timedelta(days=days)
    runs_by_date: dict = {}
    strength_by_date: dict = {}

    if STRAVA_CSV.exists():
        try:
            with open(STRAVA_CSV, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                try:
                    header = next(reader)
                except StopIteration:
                    header = []
                if header:
                    idx = _strava_column_index(header)
                    type_col = idx.get("Activity Type")
                    date_col = (
                        idx.get("Activity Date") if "Activity Date" in idx
                        else idx.get("Start Date")
                    )
                    dist_col = idx.get("Distance")
                    time_col = idx.get("Moving Time")
                    hr_col = idx.get("Average Heart Rate")
                    for row in reader:
                        try:
                            if type_col is None or type_col >= len(row):
                                continue
                            if row[type_col].strip().lower() not in ("run", "running"):
                                continue
                            if date_col is None or date_col >= len(row):
                                continue
                            act_date = _parse_strava_date(row[date_col])
                            if act_date is None or not (cutoff < act_date <= today):
                                continue
                            distance_km = None
                            if dist_col is not None and dist_col < len(row):
                                raw_str = row[dist_col].strip()
                                if raw_str:
                                    distance_km = _normalise_distance_km(float(raw_str))
                            if distance_km is None:
                                continue
                            moving_time_s = 0.0
                            if time_col is not None and time_col < len(row):
                                moving_time_s = _parse_duration_seconds(row[time_col])
                            pace = (moving_time_s / 60) / distance_km if distance_km > 0 else None
                            hr = None
                            if hr_col is not None and hr_col < len(row):
                                hr_raw = row[hr_col].strip()
                                if hr_raw:
                                    hr = float(hr_raw)
                            runs_by_date[act_date] = {
                                "distance_km": distance_km,
                                "pace_min_km": round(pace, 2) if pace else None,
                                "hr": hr,
                            }
                        except Exception:
                            pass
        except Exception:
            pass

    if STRONG_CSV.exists():
        raw: dict = {}
        try:
            with open(STRONG_CSV, newline="", encoding="utf-8-sig") as f:
                reader_s = csv.DictReader(f)
                for row in reader_s:
                    try:
                        date_str = (row.get("Date") or "")[:10]
                        act_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        if not (cutoff < act_date <= today):
                            continue
                        exercise = (row.get("Exercise Name") or "").strip()
                        weight = float(row.get("Weight") or 0)
                        reps = int(float(row.get("Reps") or 0))
                        raw.setdefault(act_date, {}).setdefault(exercise, []).append(
                            (weight, reps)
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        for act_date, exercises in raw.items():
            top_sets = [
                (ex, max(sets, key=lambda s: s[0]))
                for ex, sets in exercises.items()
            ]
            strength_by_date[act_date] = [
                (ex, best[0], best[1]) for ex, best in sorted(top_sets)
            ]

    return {"runs_by_date": runs_by_date, "strength_by_date": strength_by_date}
