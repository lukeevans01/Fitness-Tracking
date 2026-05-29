#!/usr/bin/env python3
"""WeeklyLoad — a deterministic, cross-domain view of recent training and nutrition load.

This is the shared structured input the coach uses to make a single session (or the
weekly review) aware of the whole picture: how much running and lifting has happened,
when the last hard effort was, and whether fuelling has kept up. No LLM is involved in
building it; it only feeds the LLM better inputs plus one or two hard rules. The taper
block remains the highest-priority injected rule and is unaffected by this.

Build with build_weekly_load(); inject with WeeklyLoad.to_prompt_block().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import store
import training_summary as ts
from training_summary import _MODERATE_MIN_PACE, _match_key_lift

_TZ_AMS = ZoneInfo("Europe/Amsterdam")

# A hard session within this many days counts as "recent" for the ease-the-run rule.
_RECENT_HARD_DAYS = 2
# avg kcal below this with at least this many logged days flags a fuelling deficit.
_FUEL_DEFICIT_KCAL = 2200
_FUEL_MIN_DAYS_LOGGED = 2


@dataclass
class WeeklyLoad:
    """Deterministic snapshot of the last N days of training and nutrition load."""

    run_sessions: int
    run_km: float
    strength_sessions: int
    strength_tonnage: float
    squash_sessions: int
    days_since_last_hard: int | None
    avg_protein_g: float
    avg_kcal: float
    days_logged: int

    def _fuelling_deficit(self) -> bool:
        return (
            self.days_logged >= _FUEL_MIN_DAYS_LOGGED
            and 0 < self.avg_kcal < _FUEL_DEFICIT_KCAL
        )

    def _recent_hard(self) -> bool:
        return (
            self.days_since_last_hard is not None
            and self.days_since_last_hard <= _RECENT_HARD_DAYS
        )

    def to_prompt_block(self) -> str:
        """Render the load as a prompt fragment: figures plus any derived hard rules.

        Rules are deterministic (computed from the figures, not the LLM). British spelling
        throughout since coach notes echo this copy back to Luke.
        """
        since = (
            f"{self.days_since_last_hard}"
            if self.days_since_last_hard is not None
            else "unknown"
        )
        tonnage = f"{self.strength_tonnage:,.0f} kg" if self.strength_tonnage else "n/a"
        lines = [
            "WEEKLY LOAD (last 7 days, computed from logged data):",
            f"- Runs: {self.run_sessions} session(s), {self.run_km:.1f} km total",
            f"- Strength: {self.strength_sessions} session(s), key-lift tonnage {tonnage}",
            f"- Squash: {self.squash_sessions} session(s)",
            f"- Days since last hard session: {since}",
        ]
        if self.days_logged:
            lines.append(
                f"- Nutrition: {self.days_logged}/7 days logged, "
                f"avg {self.avg_protein_g:.0f}g protein, avg {self.avg_kcal:.0f} kcal"
            )
        else:
            lines.append("- Nutrition: no days logged this week")

        rules: list[str] = []
        if self._recent_hard():
            rules.append(
                "- A hard session was completed in the last 48 hours. If today's session "
                "is a run, keep it genuinely easy (Z2, conversational); do not add volume "
                "or intensity. If it is strength, favour recovery over progression."
            )
        if self._fuelling_deficit():
            rules.append(
                "- Recent fuelling looks low (average calories under target across logged "
                "days). Soften intensity rather than pushing hard sessions, and flag the "
                "under-fuelling briefly in the coach note."
            )
        if rules:
            lines.append("Hard rules derived from this load (apply unless the taper block overrides):")
            lines.extend(rules)
        return "\n".join(lines)


def _today() -> date:
    return datetime.now(_TZ_AMS).date()


def _days_since_last_hard(breakdown: dict, today: date) -> int | None:
    """Most recent quality run (pace < moderate boundary) or key-lift strength day.

    Returns whole days since that date, or None if no hard session is found in the window.
    """
    hard_dates: list[date] = []
    for d, info in breakdown.get("runs_by_date", {}).items():
        pace = info.get("pace_min_km")
        if pace is not None and pace < _MODERATE_MIN_PACE:
            hard_dates.append(d)
    for d, entries in breakdown.get("strength_by_date", {}).items():
        if any(_match_key_lift(ex) is not None for ex, _w, _r in entries):
            hard_dates.append(d)
    if not hard_dates:
        return None
    return (today - max(hard_dates)).days


def build_weekly_load(
    days: int = 7,
    today: date | None = None,
    profile_id: str | None = None,
) -> WeeklyLoad:
    """Compute a WeeklyLoad from Strava/Strong CSVs and the nutrition store.

    Deterministic — no LLM. profile_id selects the nutrition profile; defaults to the
    active profile when omitted (resolved lazily to avoid a hard import cycle).
    """
    if today is None:
        today = _today()
    if profile_id is None:
        from profile import default_profile
        profile_id = default_profile().id

    stats = ts.build_stats(days=days, today=today)
    breakdown = ts.build_daily_breakdown(days=days, today=today)
    tonnage = ts.build_strength_tonnage(days=days, today=today)
    squash = ts.build_squash_sessions(days=days, today=today)

    weekly = store.weekly_nutrition(profile_id, today.isoformat(), days=days)
    logged_days = [d for d in weekly["days"] if d["logged"] and d["totals"]]
    days_logged = len(logged_days)
    if days_logged:
        avg_protein = sum(d["totals"].get("protein_g", 0.0) for d in logged_days) / days_logged
        avg_kcal = sum(d["totals"].get("kcal", 0.0) for d in logged_days) / days_logged
    else:
        avg_protein = 0.0
        avg_kcal = 0.0

    return WeeklyLoad(
        run_sessions=stats["run_sessions"],
        run_km=stats["run_km_total"],
        strength_sessions=stats["strength_sessions"],
        strength_tonnage=tonnage,
        squash_sessions=squash,
        days_since_last_hard=_days_since_last_hard(breakdown, today),
        avg_protein_g=round(avg_protein, 1),
        avg_kcal=round(avg_kcal, 1),
        days_logged=days_logged,
    )
