#!/usr/bin/env python3
"""Build a compact training summary from Strava and Strong CSVs for the Gemini prompt."""

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
STRAVA_CSV = DATA_DIR / "strava.csv"
STRONG_CSV = DATA_DIR / "strong.csv"

# min/km pace boundaries for bucket classification
_EASY_MIN_PACE = 6.0      # ≥ 6:00/km = easy
_MODERATE_MIN_PACE = 5.1  # 5:06–5:59/km = moderate; below = quality/MP+

_KEY_LIFTS = {
    "back squat", "barbell bench press", "romanian deadlift",
    "overhead press", "standing overhead press",
    "pull-up", "pull up", "weighted pull-up",
}


def _parse_strava_date(value: str) -> date | None:
    """Parse Strava activity date — tries ISO then the 'May 24, 2026, 5:31:14 AM' export format."""
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
    """Parse 'HH:MM:SS' or raw seconds string to float seconds."""
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


def build_stats(days: int = 7) -> dict:
    """Return structured training stats for the last `days` days.

    Keys: run_sessions, run_km_total, strength_sessions.
    Used to update adaptation_state.md weekly counters.
    """
    cutoff = date.today() - timedelta(days=days)
    run_sessions = 0
    run_km_total = 0.0
    strength_sessions = 0

    if STRAVA_CSV.exists():
        try:
            with open(STRAVA_CSV, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get("Activity Type") or "").strip().lower() not in ("run", "running"):
                        continue
                    date_str = row.get("Activity Date") or row.get("Start Date") or ""
                    act_date = _parse_strava_date(date_str)
                    if act_date is None or act_date < cutoff:
                        continue
                    dist = float(row.get("Distance") or 0)
                    run_km_total += dist if dist < 200 else dist / 1000
                    run_sessions += 1
        except Exception:
            pass

    if STRONG_CSV.exists():
        seen_dates: set[date] = set()
        try:
            with open(STRONG_CSV, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    date_str = (row.get("Date") or "")[:10]
                    try:
                        act_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if act_date < cutoff or act_date in seen_dates:
                        continue
                    seen_dates.add(act_date)
                    strength_sessions += 1
        except Exception:
            pass

    return {
        "run_sessions": run_sessions,
        "run_km_total": round(run_km_total, 1),
        "strength_sessions": strength_sessions,
    }


def build_summary(days: int = 14) -> str:
    cutoff = date.today() - timedelta(days=days)
    lines = []

    # ── Strava runs ──────────────────────────────────────────────────────
    if STRAVA_CSV.exists():
        runs = []
        try:
            with open(STRAVA_CSV, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    activity_type = (row.get("Activity Type") or "").strip().lower()
                    if activity_type not in ("run", "running"):
                        continue
                    date_str = (
                        row.get("Activity Date")
                        or row.get("Start Date")
                        or ""
                    )
                    act_date = _parse_strava_date(date_str)
                    if act_date is None:
                        continue
                    if act_date < cutoff:
                        continue
                    distance_raw = float(row.get("Distance") or 0)
                    # Strava exports distance in km; guard against metres
                    distance_km = distance_raw if distance_raw < 200 else distance_raw / 1000
                    moving_time_s = _parse_duration_seconds(row.get("Moving Time") or "0")
                    pace = (moving_time_s / 60) / distance_km if distance_km > 0 else None
                    hr_raw = row.get("Average Heart Rate") or row.get("Avg HR") or ""
                    runs.append({
                        "date": act_date,
                        "distance_km": round(distance_km, 1),
                        "pace": pace,
                        "hr": float(hr_raw) if hr_raw else None,
                    })
        except Exception as exc:
            lines.append(f"[warning: could not parse strava.csv: {exc}]")

        if runs:
            total_km = sum(r["distance_km"] for r in runs)
            easy = sum(1 for r in runs if r["pace"] and r["pace"] >= _EASY_MIN_PACE)
            moderate = sum(1 for r in runs if r["pace"] and _MODERATE_MIN_PACE <= r["pace"] < _EASY_MIN_PACE)
            quality = sum(1 for r in runs if r["pace"] and r["pace"] < _MODERATE_MIN_PACE)
            longest = max(runs, key=lambda r: r["distance_km"])
            lines.append(f"RUNS (last {days} days): {len(runs)} runs, {total_km:.1f} km total")
            lines.append(
                f"  Pace distribution — easy (≥6:00/km): {easy}, "
                f"moderate (5:06–5:59/km): {moderate}, quality (<5:06/km): {quality}"
            )
            pace_note = f" at {longest['pace']:.2f} min/km" if longest["pace"] else ""
            lines.append(f"  Longest run: {longest['distance_km']} km on {longest['date']}{pace_note}")
            hr_runs = [r for r in runs if r["hr"]]
            if hr_runs:
                avg_hr = sum(r["hr"] for r in hr_runs) / len(hr_runs)
                lines.append(f"  Avg HR across runs with data: {avg_hr:.0f} bpm")
        else:
            lines.append(f"RUNS (last {days} days): none recorded in this period.")
    else:
        lines.append("RUNS: no strava.csv found in data/ — upload via Sunday reminder.")

    lines.append("")

    # ── Strong lifts ──────────────────────────────────────────────────────
    if STRONG_CSV.exists():
        sessions: dict[date, dict[str, list[tuple[float, int]]]] = {}
        try:
            with open(STRONG_CSV, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    date_str = (row.get("Date") or "")[:10]
                    try:
                        act_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if act_date < cutoff:
                        continue
                    exercise = (row.get("Exercise Name") or "").lower().strip()
                    weight = float(row.get("Weight") or 0)
                    reps = int(float(row.get("Reps") or 0))
                    sessions.setdefault(act_date, {}).setdefault(exercise, []).append((weight, reps))
        except Exception as exc:
            lines.append(f"[warning: could not parse strong.csv: {exc}]")

        if sessions:
            lines.append(f"STRENGTH (last {days} days): {len(sessions)} session(s)")
            top_sets: dict[str, tuple[float, int]] = {}
            for day_lifts in sessions.values():
                for lift, sets in day_lifts.items():
                    matched = next((k for k in _KEY_LIFTS if k in lift), None)
                    if not matched:
                        continue
                    # Heaviest single set by weight
                    best = max(sets, key=lambda s: s[0])
                    if matched not in top_sets or best[0] > top_sets[matched][0]:
                        top_sets[matched] = best
            if top_sets:
                lines.append("  Top working sets (heaviest this period):")
                for lift in sorted(top_sets):
                    weight, reps = top_sets[lift]
                    lines.append(f"    {lift.title()}: {weight} kg × {reps}")
        else:
            lines.append(f"STRENGTH (last {days} days): none recorded in this period.")
    else:
        lines.append("STRENGTH: no strong.csv found in data/ — upload via Sunday reminder.")

    return "\n".join(lines)
