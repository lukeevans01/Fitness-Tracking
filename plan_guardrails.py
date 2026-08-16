#!/usr/bin/env python3
"""Deterministic safety envelope for an LLM-proposed training week.

The coach decides *what* to train; this module decides whether the proposal is safe to
write. Keeping the two apart is the point: targets should be recalibrated every week from
real data, but the limits they must respect should not move unless we deliberately move
them. Hard-coding the targets is what made the plan brittle; hard-coding the bounds is
what makes handing the targets to an LLM survivable.

Nothing here calls an LLM, so it runs identically in the Sunday review (which has a Gemini
key) and the daily sender (which does not).

Two severities:
  errors   the proposal is malformed or unsafe in a way that cannot be repaired
           (bad shape, unknown session kind, a date outside the target week). The week is
           rejected and the caller keeps the existing template.
  notes    the proposal was structurally fine but breached a load ceiling, so it was
           clamped down. The week is still applied, and the note explains the change.

Clamping only ever reduces load. A proposal is never scaled up to meet a target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

VALID_KINDS = {"run", "strength", "rest"}
DAYS_IN_WEEK = 7

# Load ceilings. Deliberately looser than the progression ramp: this is the line past
# which a week is unsafe, not the line the plan aims at. The coach is free to sit anywhere
# below it and to justify sitting lower.
MAX_WEEKLY_JUMP_PCT = 0.20      # vs last week's actual running volume
MAX_LONG_RUN_SHARE = 0.60       # of the proposed week's running volume
MAX_LONG_RUN_KM = 32.0
MAX_WEEKLY_KM = 90.0            # absolute backstop
MAX_SESSION_MINUTES = 300
MIN_REST_DAYS = 1               # at least one rest day in any seven

# Inside the taper, load must not rise at all.
TAPER_DAYS = 28


@dataclass
class WeekVerdict:
    """Outcome of validating a proposed week."""

    days: list = field(default_factory=list)   # the (possibly clamped) sessions
    notes: list = field(default_factory=list)  # clamps applied, human readable
    errors: list = field(default_factory=list)  # fatal problems; days is empty if set

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.errors:
            return "Rejected: " + "; ".join(self.errors)
        if self.notes:
            return "Applied with adjustments: " + "; ".join(self.notes)
        return "Applied as proposed."


def _run_km(session: dict) -> float:
    """Planned running distance for a session, 0.0 for anything that is not a run."""
    if session.get("session_kind") != "run":
        return 0.0
    value = (session.get("run_details") or {}).get("distance_km")
    if value is None:
        return 0.0
    try:
        km = float(value)
    except (TypeError, ValueError):
        return 0.0
    return km if km > 0 else 0.0


def week_running_km(days: list) -> float:
    return round(sum(_run_km(d) for d in days), 1)


def _scale_running(days: list, factor: float) -> None:
    """Scale every run's distance_km in place. Only ever called with factor < 1."""
    for session in days:
        km = _run_km(session)
        if km <= 0:
            continue
        session["run_details"]["distance_km"] = round(km * factor, 1)


def validate_week(
    proposed: object,
    week_start: date,
    *,
    last_week_km: float | None = None,
    race_date: date | None = None,
    today: date | None = None,
) -> WeekVerdict:
    """Check an LLM-proposed week and return it clamped to the safety envelope.

    proposed      list of seven session dicts, each carrying an ISO "date".
    week_start    Monday of the week the proposal is for.
    last_week_km  actual running volume of the week just gone, if known. Used for the
                  week-on-week ceiling; skipped when None so a first run is not blocked.
    race_date     enables the taper rule.
    today         defaults to week_start; used only to reject a week already in the past.
    """
    verdict = WeekVerdict()
    today = today or week_start

    if not isinstance(proposed, list):
        verdict.errors.append("proposed week must be a list of sessions")
        return verdict
    if len(proposed) != DAYS_IN_WEEK:
        verdict.errors.append(f"expected {DAYS_IN_WEEK} sessions, got {len(proposed)}")
        return verdict

    expected_dates = [(week_start + timedelta(days=i)).isoformat() for i in range(DAYS_IN_WEEK)]
    days: list = []

    for index, raw in enumerate(proposed):
        iso = expected_dates[index]
        if not isinstance(raw, dict):
            verdict.errors.append(f"{iso}: session must be an object")
            continue

        session = dict(raw)
        if session.get("date") != iso:
            verdict.errors.append(f"session {index + 1} should be dated {iso}, got {session.get('date')!r}")
            continue
        if session.get("session_kind") not in VALID_KINDS:
            verdict.errors.append(f"{iso}: session_kind must be one of {sorted(VALID_KINDS)}")
            continue
        if not session.get("session_type"):
            verdict.errors.append(f"{iso}: session_type is required")
            continue

        duration = session.get("duration_min")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                verdict.errors.append(f"{iso}: duration_min must be a whole number of minutes")
                continue
            if duration < 0:
                verdict.errors.append(f"{iso}: duration_min cannot be negative")
                continue
            if duration > MAX_SESSION_MINUTES:
                verdict.notes.append(f"{iso}: capped {duration} min to {MAX_SESSION_MINUTES}")
                duration = MAX_SESSION_MINUTES
            session["duration_min"] = duration

        if session.get("session_kind") == "run":
            session["run_details"] = dict(session.get("run_details") or {})

        days.append(session)

    if verdict.errors:
        return verdict

    if expected_dates[-1] < today.isoformat():
        verdict.errors.append(f"week beginning {week_start} is already in the past")
        return verdict

    if sum(1 for d in days if d.get("session_kind") == "rest") < MIN_REST_DAYS:
        verdict.errors.append(f"a week needs at least {MIN_REST_DAYS} rest day")
        return verdict

    # ---- load ceilings: clamp rather than reject ----
    total = week_running_km(days)

    ceilings: list = [(MAX_WEEKLY_KM, f"absolute weekly ceiling {MAX_WEEKLY_KM:.0f} km")]
    if last_week_km and last_week_km > 0:
        allowed = last_week_km * (1 + MAX_WEEKLY_JUMP_PCT)
        ceilings.append((allowed, f"{MAX_WEEKLY_JUMP_PCT:.0%} above last week's {last_week_km:.0f} km"))
    if race_date is not None and 0 <= (race_date - week_start).days <= TAPER_DAYS:
        if last_week_km and last_week_km > 0:
            ceilings.append((last_week_km, "taper: load must not rise"))

    limit, reason = min(ceilings, key=lambda pair: pair[0])
    if total > limit > 0:
        _scale_running(days, limit / total)
        verdict.notes.append(f"running cut from {total:.0f} to {limit:.0f} km ({reason})")
        total = week_running_km(days)

    # ---- long run cannot dominate the week ----
    runs = [d for d in days if _run_km(d) > 0]
    if runs:
        longest = max(runs, key=_run_km)
        long_km = _run_km(longest)
        support_km = total - long_km

        # Solve for the allowance rather than taking a share of the proposed total:
        # cutting the long run also shrinks the total, so a naive share would still leave
        # the long run over the limit. Want long / (support + long) <= s, so
        # long <= s * support / (1 - s).
        # With no other running the share is undefined and scaling cannot repair the
        # week, so only the absolute cap applies.
        share = MAX_LONG_RUN_SHARE
        if support_km <= 0:
            allowed = MAX_LONG_RUN_KM
        else:
            allowed = min(MAX_LONG_RUN_KM, share * support_km / (1 - share))

        if long_km > allowed:
            # A mild breach is a magnitude problem and is safe to clamp. A severe one means
            # the week's structure is wrong - a long run with almost no support volume - and
            # quietly rewriting it would hide that from the coach.
            if allowed <= 0 or long_km > allowed * 2:
                verdict.errors.append(
                    f"{longest['date']}: long run of {long_km:.0f} km is unsupported by only "
                    f"{support_km:.0f} km of other running (max {MAX_LONG_RUN_SHARE:.0%} of the "
                    f"week allows {allowed:.0f} km). Raise the rest of the week or shorten it."
                )
                return verdict
            longest["run_details"]["distance_km"] = round(allowed, 1)
            verdict.notes.append(
                f"{longest['date']}: long run cut from {long_km:.0f} to {allowed:.0f} km "
                f"(max {MAX_LONG_RUN_SHARE:.0%} of the week's running)"
            )

    verdict.days = days
    return verdict
