#!/usr/bin/env python3
"""Shared training-cycle date maths.

The plan is a repeating N-day blueprint with no absolute dates: a fixed
`cycle_start_date` plus `cycle_length_days` (currently 7, Monday-anchored). Any
calendar date maps deterministically onto a day in that cycle by offset from the
start. This helper is the single source of that mapping so the email coach
(process_replies / send_daily) and the dashboard builder (build_data) agree.
"""

from __future__ import annotations

from datetime import date


def cycle_day(target_date: date, plan: dict) -> tuple[int, dict, dict]:
    """Return (day_num, session, day_after_session) for target_date.

    `plan` must expose `cycle_start_date` (ISO string), `cycle_length_days` (int)
    and `cycle_days` (list of day dicts each with a 1-based `day_num`).
    """
    start = date.fromisoformat(plan["cycle_start_date"])
    cycle = plan["cycle_length_days"]
    days_in = (target_date - start).days
    day_num = (days_in % cycle) + 1
    day_after_num = ((days_in + 1) % cycle) + 1
    session = next(d for d in plan["cycle_days"] if d["day_num"] == day_num)
    day_after = next(d for d in plan["cycle_days"] if d["day_num"] == day_after_num)
    return day_num, session, day_after
