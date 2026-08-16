#!/usr/bin/env python3
"""Single place a proposed training week gets validated and written.

Three callers need this: the automatic Sunday review, an A/B/C email reply, and the
interactive CLI. They had begun to grow their own copies of the same loop, which is exactly
how the paths drift, so the validate-then-write step lives here once.

Validation itself is plan_guardrails; this module owns only the store writes and the
human-readable account of what happened.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import plan_guardrails
import store

TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def apply_week(
    profile_id: str,
    proposed: object,
    week_start: date,
    *,
    today: date,
    source: str,
    last_week_km: "float | None" = None,
    race_date: "date | None" = None,
) -> "tuple[int, plan_guardrails.WeekVerdict, str]":
    """Validate `proposed` and write the surviving days as per-date overrides.

    Returns (days_written, verdict, message). Nothing is written when the verdict fails, so
    a rejected week leaves the existing plan untouched rather than half-applied.

    Days already in the past are skipped: confirming late in the week must not rewrite
    sessions that have already been trained.
    """
    if not proposed:
        return 0, plan_guardrails.WeekVerdict(), (
            "No structured plan on this option, so the standard cycle stands."
        )

    verdict = plan_guardrails.validate_week(
        proposed,
        week_start,
        last_week_km=last_week_km,
        race_date=race_date,
        today=today,
    )
    if not verdict.ok:
        return 0, verdict, (
            "The proposed week did not pass the safety checks, so the standard cycle "
            f"stands. Reason: {'; '.join(verdict.errors)}"
        )

    applied_at = datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds")
    written = 0
    for session in verdict.days:
        iso = session["date"]
        if iso < today.isoformat():
            continue
        store.set_override(profile_id, iso, {
            "applied_at": applied_at,
            "edit_source": source,
            "session": {k: v for k, v in session.items() if k != "date"},
        })
        written += 1

    message = f"{written} of 7 days written to your plan."
    if verdict.notes:
        message += " Adjusted for safety: " + "; ".join(verdict.notes) + "."
    return written, verdict, message


def last_week_running_km(week_start: date, strava_csv) -> "float | None":
    """Actual running volume for the seven days before `week_start`, or None if unknown.

    None means "no history", which plan_guardrails treats as "skip the week-on-week
    ceiling" rather than blocking the week outright.
    """
    try:
        from ingest import get_reader
        activities = get_reader("activities")(strava_csv)
    except Exception as exc:
        print(f"[warn] could not read activities for the load ceiling: {exc}")
        return None

    previous_start = week_start - timedelta(days=7)
    km = sum(
        a.distance_km for a in activities
        if a.kind == "run" and previous_start <= a.date < week_start
    )
    return round(km, 1) if km > 0 else None
