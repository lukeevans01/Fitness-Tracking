#!/usr/bin/env python3
"""Marathon progression engine — phases the plan toward race day.

Deterministic, no LLM. Given today and the race date it reports the current training
block (base / build / taper) and the parameters that scale within it: weekly running
volume, the long-run distance and the weekly quality (marathon-pace / threshold) minutes.
The daily renderer uses these to escalate the long-run and quality-run days; the Sunday
review injects the block label so the options stay coherent with the build.

Weekly volume is the primary variable and the long run is derived from it. That ordering
is deliberate: a long run that outgrows the volume supporting it is the classic way to
pick up an overuse injury, so the long run is capped as a share of the week rather than
escalated on its own schedule.

Volume is anchored to a measured starting point (a real recent week) rather than
back-computed from race day. Anchoring to the calendar alone means the plan prescribes the
same load whether the athlete has been training or has had eleven weeks off, which is
exactly the failure this module used to have. The anchor is explicit rather than recomputed
from recent logs each week, so one disrupted week cannot ratchet the whole plan downward.

Progression theory applied here:
  - Volume rises geometrically from the anchor to a peak, three weeks up then one down
    at 85 percent. Roughly 5 percent per up-week, inside the conventional 10 percent
    ceiling and appropriate for an experienced runner returning to a known load.
  - The peak is a return to previously demonstrated volume, not new territory.
  - The long run is capped at a share of weekly volume, set from the athlete's own
    history rather than a textbook figure.
  - The final four weeks defer entirely to the existing taper rules; progression never
    escalates once the taper block is active.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

# Block boundaries, expressed in whole weeks remaining until race day.
_TAPER_WEEKS = 4    # <= 4 weeks out: defer to the taper rules
_BUILD_WEEKS = 14   # 5..14 weeks out: build (MP/threshold, escalating volume)

_PEAK_WEEK = 5            # weeks-to-race at which volume peaks
_DELOAD_FACTOR = 0.85     # down-week step back
_DELOAD_EVERY = 4         # three weeks up, then one down

# Long run as a share of weekly volume. 0.55 is set from Luke's own training history
# (median 51 percent, and 34 km inside a 56 km week during the 3:28 build), not from the
# 30-35 percent figure quoted for high-mileage runners, which does not describe him.
_LONG_RUN_SHARE = 0.55
_MAX_LONG_RUN_KM = 30.0   # ~3 hours on feet; beyond this the cost outweighs the benefit
_MIN_LONG_RUN_KM = 12.0

# Taper schedules (informational only — the taper rules own running inside four weeks).
_TAPER_LONG_RUN_FACTOR = {4: 0.70, 3: 0.55, 2: 0.45, 1: 0.30, 0: 0.0}
_TAPER_VOLUME_FACTOR = {4: 0.80, 3: 0.65, 2: 0.50, 1: 0.35, 0: 0.0}


@dataclass(frozen=True)
class VolumePlan:
    """Where weekly volume starts, and where it is heading.

    anchor_km   : measured weekly running volume at the anchor week.
    anchor_week : Monday of the week that anchor_km describes.
    peak_km     : weekly volume to peak at, one week before the taper.
    """

    anchor_km: float
    anchor_week: date
    peak_km: float

    @classmethod
    def from_profile(cls, profile) -> "VolumePlan":
        """Build from a Profile, falling back to the module defaults per field.

        The profile holds the *initial* anchor. Once the weekly review has recalibrated
        it, prefer from_store, which reads the live value.
        """
        anchor_week = getattr(profile, "weekly_volume_anchor_week", None)
        anchor_km = getattr(profile, "weekly_volume_anchor_km", None)
        peak_km = getattr(profile, "peak_weekly_km", None)
        return cls(
            anchor_km=float(anchor_km) if anchor_km else DEFAULT_VOLUME_PLAN.anchor_km,
            anchor_week=anchor_week or DEFAULT_VOLUME_PLAN.anchor_week,
            peak_km=float(peak_km) if peak_km else DEFAULT_VOLUME_PLAN.peak_km,
        )

    def merged_with_store(self, adaptation: "dict | None") -> "VolumePlan":
        """Overlay the store's recalibrated anchor on this plan, field by field.

        This is what makes the anchor stop being a hand-edited constant: the weekly review
        writes the measured value back, and every reader picks it up from here. Anything the
        store does not carry keeps this plan's value.
        """
        adaptation = adaptation or {}
        km = adaptation.get("volume_anchor_km")
        week = adaptation.get("volume_anchor_week")
        peak = adaptation.get("peak_weekly_km")
        try:
            anchor_week = date.fromisoformat(week) if week else self.anchor_week
        except (TypeError, ValueError):
            anchor_week = self.anchor_week
        return VolumePlan(
            anchor_km=float(km) if km else self.anchor_km,
            anchor_week=anchor_week,
            peak_km=float(peak) if peak else self.peak_km,
        )

    @classmethod
    def from_store(cls, profile, adaptation: "dict | None") -> "VolumePlan":
        """Convenience for callers holding a Profile: profile defaults, store overlaid."""
        return cls.from_profile(profile).merged_with_store(adaptation)

    def as_store_fields(self) -> dict:
        """The shape store.set_adaptation expects."""
        return {
            "volume_anchor_km": round(self.anchor_km, 1),
            "volume_anchor_week": self.anchor_week.isoformat(),
            "peak_weekly_km": round(self.peak_km, 1),
        }


# Fallback when a profile carries no volume anchor. Mirrors Luke's Aug 2026 restart.
DEFAULT_VOLUME_PLAN = VolumePlan(
    anchor_km=36.0,
    anchor_week=date(2026, 8, 17),
    peak_km=55.0,
)


def _monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


# Anchor recalibration. The anchor tracks measured reality so it never needs hand-editing,
# but it moves in bounded steps. An average over several weeks rather than the last week
# alone, floored so one missed week cannot collapse the plan, and capped so one big week
# cannot spike it. Without those bounds an adaptive anchor either death-spirals after a bad
# week or runs away after a good one.
ANCHOR_WEEKS = 3
ANCHOR_MAX_RISE = 1.15
ANCHOR_MAX_FALL = 0.85


def recalibrate_anchor(
    previous: VolumePlan, weekly_actuals: "list[float]", next_week: date
) -> "tuple[VolumePlan, str]":
    """Move the volume anchor toward what was actually run. Returns (plan, explanation).

    weekly_actuals  measured running km per week, oldest first. Only the most recent
                    ANCHOR_WEEKS are used. Zero weeks count: not running is real data.
    next_week       Monday the new anchor should apply from.

    The previous anchor is kept unchanged when there is no data to move it with.
    """
    recent = [float(km) for km in (weekly_actuals or [])][-ANCHOR_WEEKS:]
    if not recent:
        return (
            VolumePlan(previous.anchor_km, next_week, previous.peak_km),
            f"No recent running data, so the anchor stays at {previous.anchor_km:.0f} km.",
        )

    measured = sum(recent) / len(recent)
    floor = previous.anchor_km * ANCHOR_MAX_FALL
    ceiling = previous.anchor_km * ANCHOR_MAX_RISE
    anchor = min(ceiling, max(floor, measured))

    plan = VolumePlan(round(anchor, 1), next_week, previous.peak_km)

    detail = (
        f"{len(recent)}-week average was {measured:.0f} km against an anchor of "
        f"{previous.anchor_km:.0f} km"
    )
    if anchor >= ceiling - 0.05 and measured > ceiling:
        return plan, f"{detail}; anchor raised to {anchor:.0f} km (capped rise)."
    if anchor <= floor + 0.05 and measured < floor:
        return plan, f"{detail}; anchor eased to {anchor:.0f} km (limited fall)."
    return plan, f"{detail}; anchor now {anchor:.0f} km."


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


def is_deload_week(today: date, plan: "VolumePlan | None" = None) -> bool:
    """True on every fourth week counted from the anchor (three up, one down)."""
    plan = plan or DEFAULT_VOLUME_PLAN
    elapsed = (_monday_of(today) - _monday_of(plan.anchor_week)).days // 7
    return elapsed > 0 and (elapsed + 1) % _DELOAD_EVERY == 0


def weekly_volume_km(
    today: date, race_date: date, plan: "VolumePlan | None" = None
) -> float:
    """Target weekly running volume in km for the week containing `today`.

    Rises geometrically from the anchor to the peak across the weeks available before the
    taper, stepping back to 85 percent on every fourth week. Before the anchor week, and
    after race day, the anchor itself is returned. Inside the taper it follows a reducing
    schedule (informational; the taper rules own running there).
    """
    plan = plan or DEFAULT_VOLUME_PLAN
    weeks = weeks_to_race(today, race_date)
    if weeks < 0:
        return plan.anchor_km

    # Judge the taper from the end of the week, not `today`. The long run sits on the
    # Sunday, so a week whose Sunday is already inside the taper is a taper week for
    # reporting purposes - otherwise the footer promises a peak the sessions never carry.
    weeks_at_week_end = weeks_to_race(_monday_of(today) + timedelta(days=6), race_date)
    if weeks_at_week_end <= _TAPER_WEEKS:
        return round(plan.peak_km * _TAPER_VOLUME_FACTOR.get(max(0, weeks_at_week_end), 0.5), 1)

    elapsed = (_monday_of(today) - _monday_of(plan.anchor_week)).days // 7
    if elapsed <= 0:
        return plan.anchor_km

    # Weeks of build available between the anchor and the peak, from the anchor's own
    # position in the calendar so the ramp does not shift as `today` moves.
    anchor_weeks_out = weeks_to_race(plan.anchor_week, race_date)
    span = max(1, anchor_weeks_out - _PEAK_WEEK)
    progress = min(1.0, elapsed / span)

    km = plan.anchor_km * (plan.peak_km / plan.anchor_km) ** progress
    km = min(km, plan.peak_km)
    if is_deload_week(today, plan):
        km *= _DELOAD_FACTOR
    return round(km, 1)


def long_run_km(
    today: date, race_date: date, plan: "VolumePlan | None" = None
) -> float:
    """Target long-run distance in km for the week containing `today`.

    Derived from weekly volume rather than escalated independently, so the long run can
    never outgrow the volume supporting it. Clamped to a sane floor and to a ceiling of
    roughly three hours on feet. Inside the taper it follows the taper schedule.
    """
    plan = plan or DEFAULT_VOLUME_PLAN
    weeks = weeks_to_race(today, race_date)
    peak_long_run = min(_MAX_LONG_RUN_KM, plan.peak_km * _LONG_RUN_SHARE)

    if 0 <= weeks <= _TAPER_WEEKS:
        factor = _TAPER_LONG_RUN_FACTOR.get(weeks, 0.5)
        return round(peak_long_run * factor, 1)

    km = weekly_volume_km(today, race_date, plan) * _LONG_RUN_SHARE
    return round(min(_MAX_LONG_RUN_KM, max(_MIN_LONG_RUN_KM, km)), 1)


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


def _parse_km(text: "str | None") -> float:
    """Midpoint km from a template distance like '~7.5-8 km' or '~16 km'. 0.0 if absent."""
    if not text:
        return 0.0
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(text))]
    if not numbers:
        return 0.0
    # A range gives two numbers; take the midpoint so scaling is stable.
    return (numbers[0] + numbers[1]) / 2 if len(numbers) >= 2 else numbers[0]


def support_run_scale(
    cycle_days: list, today: date, race_date: date, plan: "VolumePlan | None" = None
) -> float:
    """Factor for the non-long running days so the week sums to the volume target.

    The long run is set by long_run_km; whatever volume remains is shared across the other
    running days in proportion to their template distances. Without this the weekly target
    would be a number in the footer that the prescribed sessions never actually add up to.

    Returns 1.0 when there is nothing to scale, or during the taper where the taper rules
    own the volume.
    """
    plan = plan or DEFAULT_VOLUME_PLAN
    weeks = weeks_to_race(today, race_date)
    if 0 <= weeks <= _TAPER_WEEKS:
        return 1.0

    support_km = sum(
        _parse_km((d.get("run_details") or {}).get("distance"))
        for d in cycle_days
        if d.get("session_kind") == "run" and not is_long_run_session(d)
    )
    if support_km <= 0:
        return 1.0

    remaining = weekly_volume_km(today, race_date, plan) - long_run_km(today, race_date, plan)
    if remaining <= 0:
        return 1.0
    # Bounded so a bad anchor cannot double an easy run overnight.
    return round(min(2.0, max(0.5, remaining / support_km)), 3)


def block_label(today: date, race_date: date, plan: "VolumePlan | None" = None) -> str:
    """Short human label for the email footer, reflecting the current block."""
    plan = plan or DEFAULT_VOLUME_PLAN
    block = block_for(today, race_date)
    weeks = weeks_to_race(today, race_date)
    volume = weekly_volume_km(today, race_date, plan)
    long_run = long_run_km(today, race_date, plan)
    deload = " Down week — hold back and let it settle." if is_deload_week(today, plan) else ""

    if block == "base":
        return (
            f"Base phase ({weeks} weeks to race) — target {volume:.0f} km this week, "
            f"long run around {long_run:.0f} km.{deload}"
        )
    if block == "build":
        return (
            f"Build phase ({weeks} weeks to race) — target {volume:.0f} km this week, "
            f"long run {long_run:.0f} km, quality work {quality_minutes(today, race_date)} min.{deload}"
        )
    return f"Taper ({weeks} weeks to race) — easy running only, the taper rules apply."


def _quality_prescription(qmin: int, marathon_pace: str | None, pace_hr: str | None) -> str:
    """Wording for the build-phase quality segment.

    With a marathon-pace target, prescribe the pace. Without one (no time goal), the
    segment is prescribed by effort and heart rate so it stays useful without pulling
    the athlete onto a pace they have not built up to.
    """
    if marathon_pace:
        hr = f", HR {pace_hr}" if pace_hr else ""
        return (
            f"Build-phase quality: include about {qmin} min at marathon pace "
            f"({marathon_pace}{hr}) split into segments, easy either side."
        )
    return (
        f"Build-phase quality: include about {qmin} min at controlled tempo effort "
        "(comfortably hard, conversational in short phrases only) split into segments, "
        "easy either side. No time goal is set, so run this by feel, not a pace."
    )


def apply_to_session(
    session: dict,
    target_date: date,
    race_date: date,
    marathon_pace: str | None = None,
    marathon_pace_hr: str | None = None,
    plan: "VolumePlan | None" = None,
    cycle_days: "list | None" = None,
) -> tuple[dict, str]:
    """Return (possibly scaled session, footer note) for the rendered daily email.

    Scales only the long-run and quality-run days, and only outside the taper — during
    the taper the static session is returned unchanged so the taper rules win (no
    progression escalation). Non-running days are returned unchanged with a block label.
    The input session is never mutated; a shallow copy is returned when scaled.
    """
    plan = plan or DEFAULT_VOLUME_PLAN
    block = block_for(target_date, race_date)
    footer = block_label(target_date, race_date, plan)
    if block == "taper":
        return session, footer

    if is_long_run_session(session):
        km = long_run_km(target_date, race_date, plan)
        minutes = _estimate_duration_min(km)
        scaled = dict(session)
        run = dict(session.get("run_details") or {})
        run["distance"] = f"~{km:.0f} km"
        run["duration"] = f"{minutes} min"
        scaled["run_details"] = run
        scaled["duration_min"] = minutes
        return scaled, footer

    scale = (
        support_run_scale(cycle_days, target_date, race_date, plan)
        if cycle_days else 1.0
    )

    if is_quality_run_session(session):
        qmin = quality_minutes(target_date, race_date)
        scaled = dict(session)
        run = dict(session.get("run_details") or {})
        _apply_support_scale(run, scaled, scale)
        if qmin > 0:
            run["effort"] = (
                _quality_prescription(qmin, marathon_pace, marathon_pace_hr)
                + " "
                + (run.get("effort") or "")
            ).strip()
        scaled["run_details"] = run
        return scaled, footer

    if session.get("session_kind") == "run" and scale != 1.0:
        scaled = dict(session)
        run = dict(session.get("run_details") or {})
        _apply_support_scale(run, scaled, scale)
        scaled["run_details"] = run
        return scaled, footer

    return session, footer


def _apply_support_scale(run: dict, scaled: dict, scale: float) -> None:
    """Rewrite a support run's distance and duration in place for `scale`."""
    if scale == 1.0:
        return
    km = _parse_km(run.get("distance"))
    if km <= 0:
        return
    new_km = round(km * scale, 1)
    run["distance"] = f"~{new_km:.0f} km" if new_km >= 10 else f"~{new_km:.1f} km"
    minutes = _estimate_duration_min(new_km)
    run["duration"] = f"{minutes} min"
    scaled["duration_min"] = minutes
