"""Tests for Pack 10 — marathon progression engine.

Deterministic; no network. All dates are relative to the San Sebastian race on
22 Nov 2026. Verifies block phasing, long-run escalation/cap/deload, that a rendered
long-run deep in the build is longer than near the start of base, and that the taper
takes precedence (no progression escalation inside the final four weeks).

Run from the fitness-emails dir:  python3 -m unittest tests.test_progression
"""

import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import progression  # noqa: E402

RACE = date(2026, 11, 22)

# Representative dates (weeks-to-race via ceil(days/7)):
BASE_DATE = date(2026, 6, 1)    # ~25 weeks out -> base
BUILD_DATE = date(2026, 9, 1)   # ~12 weeks out -> build
TAPER_DATE = date(2026, 11, 1)  # ~3 weeks out -> taper

PEAK_DATE = date(2026, 10, 22)  # 31 days out -> 5 weeks -> peak long run
DELOAD_W8 = date(2026, 9, 27)   # 56 days out -> 8 weeks (deload)
NORMAL_W9 = date(2026, 9, 20)   # 63 days out -> 9 weeks


def _long_run_template() -> dict:
    return {
        "session_type": "Long Run - Aerobic Base",
        "session_kind": "run",
        "duration_min": 90,
        "run_details": {"distance": "~15-16 km", "duration": "90 min",
                        "pace": "5:25-5:45/km", "hr_target": "150-160", "effort": "GA"},
        "short_version": "Drop to 60 min.",
    }


def _km(session: dict) -> float:
    m = re.search(r"([\d.]+)", session["run_details"]["distance"])
    return float(m.group(1))


class BlockForTests(unittest.TestCase):
    def test_base_build_taper(self):
        self.assertEqual(progression.block_for(BASE_DATE, RACE), "base")
        self.assertEqual(progression.block_for(BUILD_DATE, RACE), "build")
        self.assertEqual(progression.block_for(TAPER_DATE, RACE), "taper")

    def test_after_race_is_base(self):
        self.assertEqual(progression.block_for(date(2026, 11, 30), RACE), "base")

    def test_taper_boundary_aligns_with_28_days(self):
        # 28 days out == 4 weeks == taper; 29 days out == build.
        self.assertEqual(progression.block_for(date(2026, 10, 25), RACE), "taper")
        self.assertEqual(progression.block_for(date(2026, 10, 24), RACE), "build")


# A volume plan matching Luke's Aug 2026 restart, used by the volume tests.
PLAN = progression.VolumePlan(anchor_km=36.0, anchor_week=date(2026, 8, 17), peak_km=55.0)
# Loading weeks relative to that anchor (elapsed 0, 1, 2), then the first down week.
LOAD_W0 = date(2026, 8, 17)
LOAD_W1 = date(2026, 8, 24)
LOAD_W2 = date(2026, 8, 31)
DOWN_W3 = date(2026, 9, 7)   # elapsed 3 -> three up, one down
LOAD_W4 = date(2026, 9, 14)


class WeeklyVolumeTests(unittest.TestCase):
    def test_starts_at_the_anchor(self):
        self.assertEqual(progression.weekly_volume_km(LOAD_W0, RACE, PLAN), 36.0)

    def test_before_the_anchor_holds_at_the_anchor(self):
        """A date earlier than the anchor must not extrapolate backwards."""
        self.assertEqual(progression.weekly_volume_km(BASE_DATE, RACE, PLAN), 36.0)

    def test_rises_across_loading_weeks(self):
        vols = [progression.weekly_volume_km(d, RACE, PLAN) for d in (LOAD_W0, LOAD_W1, LOAD_W2)]
        self.assertEqual(vols, sorted(vols))
        self.assertGreater(vols[-1], vols[0])

    def test_down_week_steps_back(self):
        self.assertLess(
            progression.weekly_volume_km(DOWN_W3, RACE, PLAN),
            progression.weekly_volume_km(LOAD_W2, RACE, PLAN),
        )

    def test_three_up_one_down_cadence(self):
        flags = [progression.is_deload_week(d, PLAN)
                 for d in (LOAD_W0, LOAD_W1, LOAD_W2, DOWN_W3, LOAD_W4)]
        self.assertEqual(flags, [False, False, False, True, False])

    def test_never_exceeds_the_peak(self):
        d = PLAN.anchor_week
        while d < RACE:
            self.assertLessEqual(progression.weekly_volume_km(d, RACE, PLAN), PLAN.peak_km)
            d += timedelta(days=7)

    def test_loading_week_rise_stays_inside_the_ten_percent_guardrail(self):
        """Compared against the previous *loading* week, not a recovery week."""
        loads = []
        d = PLAN.anchor_week
        while progression.weeks_to_race(d, RACE) > progression._TAPER_WEEKS:
            if not progression.is_deload_week(d, PLAN):
                loads.append(progression.weekly_volume_km(d, RACE, PLAN))
            d += timedelta(days=7)
        for previous, nxt in zip(loads, loads[1:]):
            self.assertLessEqual((nxt - previous) / previous, 0.10)


class LongRunTests(unittest.TestCase):
    def test_derived_from_weekly_volume(self):
        volume = progression.weekly_volume_km(LOAD_W0, RACE, PLAN)
        self.assertAlmostEqual(
            progression.long_run_km(LOAD_W0, RACE, PLAN),
            round(volume * progression._LONG_RUN_SHARE, 1),
            places=1,
        )

    def test_never_exceeds_the_share_of_the_week(self):
        d = PLAN.anchor_week
        while progression.weeks_to_race(d, RACE) > progression._TAPER_WEEKS:
            volume = progression.weekly_volume_km(d, RACE, PLAN)
            self.assertLessEqual(
                progression.long_run_km(d, RACE, PLAN),
                volume * progression._LONG_RUN_SHARE + 0.1,
            )
            d += timedelta(days=7)

    def test_capped_at_three_hours_on_feet(self):
        big = progression.VolumePlan(anchor_km=90.0, anchor_week=LOAD_W0, peak_km=120.0)
        d = LOAD_W0
        while d < RACE:
            self.assertLessEqual(
                progression.long_run_km(d, RACE, big), progression._MAX_LONG_RUN_KM
            )
            d += timedelta(days=7)

    def test_respects_the_floor(self):
        tiny = progression.VolumePlan(anchor_km=8.0, anchor_week=LOAD_W0, peak_km=10.0)
        self.assertGreaterEqual(
            progression.long_run_km(LOAD_W0, RACE, tiny), progression._MIN_LONG_RUN_KM
        )

    def test_down_week_shortens_the_long_run(self):
        self.assertLess(
            progression.long_run_km(DOWN_W3, RACE, PLAN),
            progression.long_run_km(LOAD_W2, RACE, PLAN),
        )


class SupportRunScaleTests(unittest.TestCase):
    """The prescribed sessions must actually add up to the weekly target."""

    def _cycle(self):
        return [
            {"day_num": 2, "session_kind": "run", "session_type": "Easy Recovery Run",
             "run_details": {"distance": "~7.5-8 km"}},
            {"day_num": 4, "session_kind": "run", "session_type": "Quality Run - Tempo",
             "run_details": {"distance": "~10-11 km"}},
            {"day_num": 7, "session_kind": "run", "session_type": "Long Run - Aerobic Base",
             "run_details": {"distance": "~15-16 km"}},
            {"day_num": 1, "session_kind": "strength", "session_type": "Full Body"},
        ]

    def test_week_sums_to_the_volume_target(self):
        cycle = self._cycle()
        for monday in (LOAD_W0, LOAD_W1, LOAD_W2, DOWN_W3, LOAD_W4):
            scale = progression.support_run_scale(cycle, monday, RACE, PLAN)
            support = sum(
                progression._parse_km(d["run_details"]["distance"]) * scale
                for d in cycle
                if d["session_kind"] == "run" and not progression.is_long_run_session(d)
            )
            total = support + progression.long_run_km(monday, RACE, PLAN)
            target = progression.weekly_volume_km(monday, RACE, PLAN)
            self.assertAlmostEqual(total, target, delta=0.5, msg=f"week of {monday}")

    def test_no_scaling_without_a_cycle(self):
        self.assertEqual(progression.support_run_scale([], LOAD_W0, RACE, PLAN), 1.0)

    def test_scale_is_bounded(self):
        cycle = self._cycle()
        absurd = progression.VolumePlan(anchor_km=400.0, anchor_week=LOAD_W0, peak_km=500.0)
        self.assertLessEqual(progression.support_run_scale(cycle, LOAD_W0, RACE, absurd), 2.0)

    def test_parse_km_handles_ranges_and_singles(self):
        self.assertEqual(progression._parse_km("~7.5-8 km"), 7.75)
        self.assertEqual(progression._parse_km("~16 km"), 16.0)
        self.assertEqual(progression._parse_km(None), 0.0)
        self.assertEqual(progression._parse_km("rest"), 0.0)


class QualityMinutesTests(unittest.TestCase):
    def test_zero_in_base(self):
        self.assertEqual(progression.quality_minutes(BASE_DATE, RACE), 0)

    def test_rises_through_build(self):
        self.assertGreater(progression.quality_minutes(BUILD_DATE, RACE), 0)
        self.assertLessEqual(progression.quality_minutes(BUILD_DATE, RACE), 50)

    def test_reduced_in_taper(self):
        self.assertEqual(progression.quality_minutes(TAPER_DATE, RACE), 15)


class ApplyToSessionTests(unittest.TestCase):
    def test_rendered_long_run_grows_from_base_to_build(self):
        base_session, _ = progression.apply_to_session(_long_run_template(), BASE_DATE, RACE)
        build_session, _ = progression.apply_to_session(_long_run_template(), date(2026, 10, 15), RACE)
        self.assertGreater(_km(build_session), _km(base_session))
        self.assertGreater(build_session["duration_min"], base_session["duration_min"])

    def test_taper_leaves_session_unchanged(self):
        original = _long_run_template()
        scaled, footer = progression.apply_to_session(original, TAPER_DATE, RACE)
        # Taper wins: the static template distance is untouched, no progression escalation.
        self.assertEqual(scaled["run_details"]["distance"], "~15-16 km")
        self.assertIn("Taper", footer)

    def test_does_not_mutate_input(self):
        original = _long_run_template()
        progression.apply_to_session(original, BUILD_DATE, RACE)
        self.assertEqual(original["run_details"]["distance"], "~15-16 km")

    def test_non_running_day_returns_unchanged_with_label(self):
        strength = {"session_type": "Strength A", "session_kind": "strength", "duration_min": 60}
        scaled, footer = progression.apply_to_session(strength, BUILD_DATE, RACE)
        self.assertIs(scaled, strength)
        self.assertIn("Build", footer)


def _quality_template() -> dict:
    return {
        "session_type": "Quality Run - Easy with Optional Tempo Segment",
        "session_kind": "run",
        "duration_min": 60,
        "run_details": {"distance": "~10-11 km", "pace": "5:25-5:45/km", "effort": ""},
    }


class QualityPrescriptionTests(unittest.TestCase):
    """A marathon pace is prescribed only when the profile actually sets a time goal."""

    def test_pace_used_when_profile_supplies_one(self):
        scaled, _ = progression.apply_to_session(
            _quality_template(), BUILD_DATE, RACE,
            marathon_pace="4:51/km", marathon_pace_hr="165-170",
        )
        effort = scaled["run_details"]["effort"]
        self.assertIn("4:51/km", effort)
        self.assertIn("165-170", effort)
        self.assertIn("marathon pace", effort)

    def test_effort_based_when_no_time_goal(self):
        scaled, _ = progression.apply_to_session(_quality_template(), BUILD_DATE, RACE)
        effort = scaled["run_details"]["effort"]
        self.assertNotIn("4:51", effort)
        self.assertNotIn("marathon pace", effort)
        self.assertIn("tempo effort", effort)
        self.assertIn("No time goal", effort)

    def test_base_phase_adds_no_quality_segment(self):
        scaled, _ = progression.apply_to_session(_quality_template(), BASE_DATE, RACE)
        self.assertEqual(scaled["run_details"]["effort"], "")


if __name__ == "__main__":
    unittest.main()


class AnchorRecalibrationTests(unittest.TestCase):
    """The anchor must track reality without death-spiralling or running away."""

    def _plan(self, km=40.0):
        return progression.VolumePlan(anchor_km=km, anchor_week=LOAD_W0, peak_km=55.0)

    def test_no_data_keeps_the_anchor(self):
        plan, note = progression.recalibrate_anchor(self._plan(), [], LOAD_W1)
        self.assertEqual(plan.anchor_km, 40.0)
        self.assertEqual(plan.anchor_week, LOAD_W1)
        self.assertIn("stays at", note)

    def test_tracks_a_steady_average(self):
        plan, note = progression.recalibrate_anchor(self._plan(), [41.0, 42.0, 43.0], LOAD_W1)
        self.assertAlmostEqual(plan.anchor_km, 42.0, places=1)
        self.assertIn("anchor now", note)

    def test_one_missed_week_cannot_collapse_the_plan(self):
        """The death spiral this guards against: a zero week halving next week's plan."""
        plan, note = progression.recalibrate_anchor(self._plan(), [40.0, 40.0, 0.0], LOAD_W1)
        self.assertAlmostEqual(plan.anchor_km, 40.0 * progression.ANCHOR_MAX_FALL, places=1)
        self.assertIn("limited fall", note)

    def test_one_huge_week_cannot_spike_the_plan(self):
        plan, note = progression.recalibrate_anchor(self._plan(), [40.0, 40.0, 200.0], LOAD_W1)
        self.assertAlmostEqual(plan.anchor_km, 40.0 * progression.ANCHOR_MAX_RISE, places=1)
        self.assertIn("capped rise", note)

    def test_sustained_detraining_does_lower_the_anchor(self):
        """Bounded, not frozen: repeated zero weeks must walk the anchor down."""
        plan = self._plan()
        for _ in range(5):
            plan, _ = progression.recalibrate_anchor(plan, [0.0, 0.0, 0.0], LOAD_W1)
        self.assertLess(plan.anchor_km, 20.0)

    def test_sustained_growth_does_raise_the_anchor(self):
        plan = self._plan()
        for _ in range(4):
            plan, _ = progression.recalibrate_anchor(plan, [80.0, 80.0, 80.0], LOAD_W1)
        self.assertGreater(plan.anchor_km, 60.0)

    def test_only_the_most_recent_weeks_count(self):
        long_history = [5.0] * 20 + [45.0, 45.0, 45.0]
        plan, _ = progression.recalibrate_anchor(self._plan(), long_history, LOAD_W1)
        self.assertGreater(plan.anchor_km, 40.0)

    def test_peak_is_preserved(self):
        plan, _ = progression.recalibrate_anchor(self._plan(), [30.0], LOAD_W1)
        self.assertEqual(plan.peak_km, 55.0)

    def test_round_trips_through_store_fields(self):
        plan, _ = progression.recalibrate_anchor(self._plan(), [43.0, 44.0, 45.0], LOAD_W1)
        restored = progression.VolumePlan(1.0, LOAD_W0, 1.0).merged_with_store(plan.as_store_fields())
        self.assertEqual(restored, plan)
