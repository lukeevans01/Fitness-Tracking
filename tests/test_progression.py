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
from datetime import date
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


class LongRunTests(unittest.TestCase):
    def test_increases_from_base_through_build(self):
        base = progression.long_run_km(BASE_DATE, RACE)
        build = progression.long_run_km(BUILD_DATE, RACE)
        self.assertGreater(build, base)

    def test_capped_at_peak(self):
        self.assertEqual(progression.long_run_km(PEAK_DATE, RACE, peak_km=32.0), 32.0)
        # Never exceeds the peak anywhere in the build.
        for d in (BUILD_DATE, DELOAD_W8, NORMAL_W9, PEAK_DATE):
            self.assertLessEqual(progression.long_run_km(d, RACE, peak_km=32.0), 32.0)

    def test_deload_week_steps_down(self):
        # The 8-weeks-out deload week is shorter than the older 9-weeks-out week.
        self.assertLess(
            progression.long_run_km(DELOAD_W8, RACE),
            progression.long_run_km(NORMAL_W9, RACE),
        )

    def test_base_holds_at_floor(self):
        self.assertEqual(progression.long_run_km(BASE_DATE, RACE, base_km=12.0), 12.0)


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


if __name__ == "__main__":
    unittest.main()
