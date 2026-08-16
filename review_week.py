#!/usr/bin/env python3
"""Interactive weekly review — the same calibration the Sunday email runs, on demand.

Reviews what was actually trained, has the coach propose three options for the coming
week, validates each proposal against the deterministic guardrails, and prints what would
change. Writes nothing unless you pass --apply.

This deliberately imports send_sunday's own helpers rather than reassembling the context.
The interactive and automated paths must not drift: if the Sunday review gains a new input,
this picks it up for free.

Usage (from the fitness-emails dir):
    python3 review_week.py                  # review and show all three options
    python3 review_week.py --apply B        # write option B's week as overrides
    python3 review_week.py --apply B --recalibrate-anchor
    python3 review_week.py --week-start 2026-08-31

Requires GEMINI_API_KEY. Never sends email and never touches the pending-choice record,
so it cannot interfere with an A/B/C reply already in flight.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import coach_orchestrator
import nutrition_logger
import plan_guardrails
import plan_writer
import progression
import store
import training_summary as ts
import weekly_load
from profile import default_profile
from send_sunday import (
    ROOT,
    TZ_AMSTERDAM,
    _compute_standard_week,
    _recent_weekly_km,
)

LETTERS = ("A", "B", "C")


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Review last week and calibrate the next one.")
    p.add_argument("--apply", metavar="LETTER", choices=[*LETTERS, *[l.lower() for l in LETTERS]],
                   help="write this option's week to the plan as per-date overrides")
    p.add_argument("--recalibrate-anchor", action="store_true",
                   help="also persist the recalculated volume anchor (Sunday does this automatically)")
    p.add_argument("--week-start", metavar="YYYY-MM-DD",
                   help="Monday the proposed week applies from (default: next Monday)")
    p.add_argument("--json", action="store_true", help="print the raw coach response and exit")
    return p.parse_args(argv)


def _default_week_start(today: date) -> date:
    """The next Monday. Reviewing on a Monday plans that same week."""
    ahead = (7 - today.weekday()) % 7
    return today + timedelta(days=ahead or 7) if today.weekday() != 0 else today


def _fmt_km(session: dict) -> str:
    km = (session.get("run_details") or {}).get("distance_km")
    return f"{float(km):.0f} km" if km else ""


def _print_option(letter: str, option: dict, verdict, recommended: bool) -> None:
    mark = "  <- coach's pick" if recommended else ""
    print(f"\n--- Option {letter}: {option.get('label', '?')}{mark}")
    print(f"    {option.get('rationale', '')}")

    if verdict is None:
        print("    No structured plan on this option, so choosing it leaves the template in place.")
        return

    if not verdict.ok:
        print("    REJECTED by guardrails, so this option cannot be applied:")
        for err in verdict.errors:
            print(f"      - {err}")
        return

    total = plan_guardrails.week_running_km(verdict.days)
    print(f"    Running: {total:.0f} km across the week")
    for session in verdict.days:
        when = date.fromisoformat(session["date"]).strftime("%a %d %b")
        extra = _fmt_km(session)
        suffix = f"  ({extra})" if extra else ""
        print(f"      {when}  {session.get('session_type', '?')}{suffix}")
    for note in verdict.notes:
        print(f"    adjusted: {note}")


def main(argv=None) -> int:
    args = _parse_args(argv)

    profile = default_profile()
    today = datetime.now(TZ_AMSTERDAM).date()
    week_start = (
        date.fromisoformat(args.week_start) if args.week_start else _default_week_start(today)
    )

    with open(ROOT / "plan_template.json") as f:
        plan = json.load(f)

    adaptation = store.get_adaptation(profile.id)
    current = progression.VolumePlan.from_store(profile, adaptation)
    volume_plan, anchor_note = progression.recalibrate_anchor(
        current, _recent_weekly_km(today), week_start
    )

    print(f"Week of {week_start:%d %b %Y}  |  {profile.race_label} in "
          f"{progression.weeks_to_race(week_start, profile.race_date)} weeks  |  "
          f"goal: {profile.race_target}")
    print(f"Volume anchor: {anchor_note}")
    if args.recalibrate_anchor:
        store.set_adaptation(profile.id, volume_plan.as_store_fields())
        print("  persisted.")
    else:
        print("  not persisted (pass --recalibrate-anchor to keep it).")

    load = weekly_load.build_weekly_load(days=7, today=today, profile_id=profile.id)
    standard_week = _compute_standard_week(today, plan, profile.race_date, volume_plan)
    training_text = ts.build_summary(days=14, today=today)
    progression_note = (
        progression.block_label(week_start, profile.race_date, volume_plan)
        + f"\nVolume anchor: {anchor_note}"
    )
    weekly_nutrition = nutrition_logger.weekly_summary(
        days=7, end_date=today, targets=profile.daily_targets, profile_id=profile.id
    )
    nutrition_text = (
        f"{weekly_nutrition['days_logged']}/7 days logged."
        if weekly_nutrition.get("days_logged") else "No nutrition logs this week."
    )

    try:
        summary = coach_orchestrator.generate_weekly_summary(
            training_summary=training_text,
            standard_week=standard_week,
            nutrition_summary=nutrition_text,
            profile=profile,
            weekly_load=load,
            progression_note=progression_note,
        )
    except Exception as exc:
        print(f"\nCoach call failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"\nReview: {summary.get('week_review', '')}")

    last_week_km = None
    recent = _recent_weekly_km(today, weeks=1)
    if recent:
        last_week_km = recent[-1] or None

    verdicts = {}
    for letter in LETTERS:
        option = summary.get(f"option_{letter.lower()}", {})
        proposed = option.get("plan")
        verdicts[letter] = (
            plan_guardrails.validate_week(
                proposed, week_start,
                last_week_km=last_week_km, race_date=profile.race_date, today=today,
            ) if proposed else None
        )
        _print_option(letter, option, verdicts[letter], summary.get("recommendation") == letter)

    print(f"\nCoach recommends {summary.get('recommendation')}: "
          f"{summary.get('recommendation_reason', '')}")
    if summary.get("coach_note"):
        print(f"Note: {summary['coach_note']}")

    improvements = summary.get("improvements", {})
    if improvements:
        print("\nOne thing to fix in each domain:")
        for domain in ("running", "lifting", "nutrition"):
            if improvements.get(domain):
                print(f"  {domain}: {improvements[domain]}")

    if not args.apply:
        print("\nNothing written. Re-run with --apply A|B|C to put a week into the plan.")
        return 0

    letter = args.apply.upper()
    verdict = verdicts.get(letter)
    if verdict is None:
        print(f"\nOption {letter} has no structured plan, so there is nothing to write.",
              file=sys.stderr)
        return 1
    if not verdict.ok:
        print(f"\nOption {letter} failed the guardrails; refusing to write it.", file=sys.stderr)
        return 1

    written, _, message = plan_writer.apply_week(
        profile.id, verdict.days, week_start,
        today=today, source="weekly_review_cli",
        last_week_km=last_week_km, race_date=profile.race_date,
    )
    print(f"\nWrote option {letter}: {message}")
    print("Commit data/app.db to make it live on the site and in the daily emails.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
