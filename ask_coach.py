#!/usr/bin/env python3
"""On-demand coach CLI — ask a training question, get advice. Changes no plan.

Examples:
    python3 ask_coach.py "should I deload bench this week given my sleep?"
    python3 ask_coach.py --domain run "is my easy pace too quick at 5:20/km?"

Domain defaults to strength. Requires GEMINI_API_KEY. This prints advice only; it
never writes an override — session changes still go through the daily-email reply flow.
"""

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import coach_orchestrator
import training_summary as ts
import weekly_load
from profile import default_profile

_TZ = ZoneInfo("Europe/Amsterdam")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the on-demand training coach.")
    parser.add_argument("question", nargs="+", help="Your training question.")
    parser.add_argument(
        "--domain", default="strength", choices=["strength", "run", "rest"],
        help="Which specialist to consult (default: strength).",
    )
    parser.add_argument(
        "--no-routines", action="store_true",
        help="Do not inject the routine template library into the prompt.",
    )
    args = parser.parse_args()
    question = " ".join(args.question).strip()
    if not question:
        print("No question provided.", file=sys.stderr)
        return 2

    profile = default_profile()
    today = datetime.now(_TZ).date()

    try:
        summary = ts.build_summary(days=14, today=today)
    except Exception as exc:  # noqa: BLE001 — summary is best-effort context
        print(f"[warn] training summary unavailable: {exc}", file=sys.stderr)
        summary = ""

    load = None
    try:
        load = weekly_load.build_weekly_load(days=7, today=today, profile_id=profile.id)
    except Exception as exc:  # noqa: BLE001 — load is best-effort context
        print(f"[warn] weekly load unavailable: {exc}", file=sys.stderr)

    try:
        answer = coach_orchestrator.answer_training_question(
            question,
            domain=args.domain,
            training_summary=summary,
            profile=profile,
            weekly_load=load,
            include_routines=not args.no_routines,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"[error] coach failed: {exc}", file=sys.stderr)
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
