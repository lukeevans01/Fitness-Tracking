#!/usr/bin/env python3
"""Marathon progression engine — phases the plan toward race day.

Deterministic, no LLM. Given today and the race date it reports the current training
block (base / build / taper) and the parameters that scale within it: the long-run
distance and the weekly quality (marathon-pace / threshold) minutes. The daily renderer
uses these to escalate the long-run and quality-run days; the Sunday review injects the
block label so the options stay coherent with the build.

The final four weeks defer entirely to the existing taper rules — progression never
escalates once the taper block is active. Escalation is conservative (no week-on-week
jump beyond ~10 percent on the long run, with a deload every fourth week) given the
new-baby sleep context.
"""

from __future__ import annotations

import math
from datetime import date

# Block boundaries, expressed in whole weeks remaining until race day.
_TAPER_WEEKS = 4    # <= 4 weeks out: defer to the taper rules
_BUILD_WEEKS = 14   # 5..14 weeks out: build (MP/threshold, escalating long run)

# Long-run escalation. Peaks in the last build week (just before the taper).
_PEAK_WEEK = 5            # weeks-to-race at which the long run peaks
_WEEKLY_RAMP = 1.08       # ~8 percent per up-week (below the ~10 percent guardrail)
_DELOAD_FACTOR = 0.85     # down-week step back
_DELOAD_EVERY = 4         # every fourth week remaining is a deload week

# Taper long-run schedule (informational only — the taper rules own running here).
_TAPER_LONG_RUN_FACTOR = {4: 0.70, 3: 0.55, 2: 0.45, 1: 0.30, 0: 0.0}


def weeks_to_race(today: date, race_date: date) -> int:
    """Whole weeks remaining until race day (rounded up so 28 days == 4 weeks).

    Rounding up aligns the 4-week taper boundary with the 28-day taper window used by
    coach_orchestrator.is_taper_active. Negative after race day.
    """
    return math.ceil((race_date - today).days / 7)


def block_for(today: date, race_date: date) -> str:
    """Return the current training block: 'base', 'build', or 'taper'.

    base  : more than 14 weeks out (and any time after race day — off-season).
    build : 5 to 14 weeks out — introduce MP/threshold and escalate the long run.
    taper : 4 weeks out or fewer — defer to the existing taper rules.
    """
    weeks = weeks_to_race(today, race_date)
    if weeks < 0:
        return "base"
    if weeks <= _TAPER_WEEKS:
        return "taper"
    if weeks <= _BUILD_WEEKS:
        return "build"
    return "base"


def long_run_km(
    today: date, race_date: date, base_km: float = 12.0, peak_km: float = 32.0
) -> float:
    """Target long-run distance in km for the week containing `today`.

    Holds at base_km deep in the base phase, escalates multiplicatively through the
    build toward peak_km in the final build week, and steps back on deload weeks. During
    the taper it follows a reducing schedule (informational; the taper rules apply the
    real running caps). Capped at peak_km.
    """
    weeks = weeks_to_race(today, race_date)
    if weeks < 0:
        return base_km
    if weeks <= _TAPER_WEEKS:
        factor = _TAPER_LONG_RUN_FACTOR.get(weeks, 0.5)
        return round(peak_km * factor, 1)

    weeks_before_peak = weeks - _PEAK_WEEK
    dist = peak_km / (_WEEKLY_RAMP ** weeks_before_peak)
    dist = max(base_km, min(peak_km, dist))
    if weeks % _DELOAD_EVERY == 0:
        dist *= _DELOAD_FACTOR
    return round(dist, 1)


def quality_minutes(today: date, race_date: date) -> int:
    """Weekly marathon-pace / threshold minutes for the quality run.

    0 during the base phase, rising linearly from 20 (14 weeks out) to 50 (5 weeks out)
    through the build, and a single short 15-minute segment during the taper.
    """
    block = block_for(today, race_date)
    if block == "base":
        return 0
    if block == "taper":
        return 15
    weeks = max(_PEAK_WEEK, min(_BUILD_WEEKS, weeks_to_race(today, race_date)))
    span = _BUILD_WEEKS - weeks  # 0 at 14 weeks, 9 at 5 weeks
    return int(round(20 + (span / (_BUILD_WEEKS - _PEAK_WEEK)) * 30))


# ──────────────────────────────────────────────────────────────────────────
# Session application
# ──────────────────────────────────────────────────────────────────────────

def is_long_run_session(session: dict) -> bool:
    return (
        session.get("session_kind") == "run"
        and "long run" in (session.get("session_type") or "").lower()
    )


def is_quality_run_session(session: dict) -> bool:
    return (
        session.get("session_kind") == "run"
        and "quality" in (session.get("session_type") or "").lower()
    )


def _estimate_duration_min(km: float, pace_min_km: float = 5.6) -> int:
    """Rough long-run duration from distance at an easy aerobic pace."""
    return int(round(km * pace_min_km))


def block_label(today: date, race_date: date) -> str:
    """Short human label for the email footer, reflecting the current block."""
    block = block_for(today, race_date)
    weeks = weeks_to_race(today, race_date)
    if block == "base":
        return f"Base phase ({weeks} weeks to race) — long run holding around {long_run_km(today, race_date):.0f} km."
    if block == "build":
        return (
            f"Build phase ({weeks} weeks to race) — long run target {long_run_km(today, race_date):.0f} km, "
            f"quality work {quality_minutes(today, race_date)} min/week."
        )
    return f"Taper ({weeks} weeks to race) — easy running only, the taper rules apply."


def apply_to_session(session: dict, target_date: date, race_date: date) -> tuple[dict, str]:
    """Return (possibly scaled session, footer note) for the rendered daily email.

    Scales only the long-run and quality-run days, and only outside the taper — during
    the taper the static session is returned unchanged so the taper rules win (no
    progression escalation). Non-running days are returned unchanged with a block label.
    The input session is never mutated; a shallow copy is returned when scaled.
    """
    block = block_for(target_date, race_date)
    footer = block_label(target_date, race_date)
    if block == "taper":
        return session, footer

    if is_long_run_session(session):
        km = long_run_km(target_date, race_date)
        minutes = _estimate_duration_min(km)
        scaled = dict(session)
        run = dict(session.get("run_details") or {})
        run["distance"] = f"~{km:.0f} km"
        run["duration"] = f"{minutes} min"
        scaled["run_details"] = run
        scaled["duration_min"] = minutes
        return scaled, footer

    if is_quality_run_session(session):
        qmin = quality_minutes(target_date, race_date)
        scaled = dict(session)
        run = dict(session.get("run_details") or {})
        if qmin > 0:
            run["effort"] = (
                f"Build-phase quality: include about {qmin} min at marathon pace "
                "(4:51/km, HR 165-170) split into segments, easy either side. "
                + (run.get("effort") or "")
            ).strip()
        scaled["run_details"] = run
        return scaled, footer

    return session, footer
