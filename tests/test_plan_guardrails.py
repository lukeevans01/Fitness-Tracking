"""Tests for the deterministic safety envelope around an LLM-proposed week.

No network, no LLM. These are the tests that make handing weekly targets to a model
survivable, so they lean towards the adversarial: malformed shapes, silly volumes, a long
run that swallows the week, load rising during a taper.

Run from the fitness-emails dir:  python3 -m unittest tests.test_plan_guardrails
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import plan_guardrails as pg  # noqa: E402

WEEK_START = date(2026, 8, 24)
RACE = date(2026, 11, 22)


def _week(kms=(8, 8, 8, 8, 10, None, None), start: date = WEEK_START) -> list:
    """Build exactly seven dated sessions from seven entries.

    Each entry is a running distance in km, or None for a rest day. Explicit rather than
    padded so a test's stated weekly volume is exactly what it builds, and dated from
    `start` so weeks other than WEEK_START are valid.
    """
    assert len(kms) == 7, "pass exactly seven entries"
    days = []
    for i, km in enumerate(kms):
        iso = (start + timedelta(days=i)).isoformat()
        if km is None:
            days.append({"date": iso, "session_type": "Rest day",
                         "session_kind": "rest", "duration_min": 30})
        else:
            days.append({"date": iso, "session_type": "Run session",
                         "session_kind": "run", "duration_min": 60,
                         "run_details": {"distance_km": float(km)}})
    return days


class ShapeTests(unittest.TestCase):
    def test_rejects_non_list(self):
        v = pg.validate_week({"nope": 1}, WEEK_START)
        self.assertFalse(v.ok)
        self.assertIn("must be a list", v.errors[0])

    def test_rejects_wrong_length(self):
        v = pg.validate_week(_week()[:5], WEEK_START)
        self.assertFalse(v.ok)
        self.assertIn("expected 7", v.errors[0])

    def test_rejects_unknown_session_kind(self):
        days = _week()
        days[0]["session_kind"] = "yoga"
        v = pg.validate_week(days, WEEK_START)
        self.assertFalse(v.ok)
        self.assertIn("session_kind", v.errors[0])

    def test_rejects_missing_session_type(self):
        days = _week()
        days[0]["session_type"] = ""
        v = pg.validate_week(days, WEEK_START)
        self.assertFalse(v.ok)

    def test_rejects_misdated_day(self):
        days = _week()
        days[3]["date"] = "2027-01-01"
        v = pg.validate_week(days, WEEK_START)
        self.assertFalse(v.ok)
        self.assertIn("should be dated", v.errors[0])

    def test_rejects_week_already_past(self):
        v = pg.validate_week(_week(), WEEK_START, today=WEEK_START + timedelta(days=30))
        self.assertFalse(v.ok)
        self.assertIn("in the past", v.errors[0])

    def test_rejects_week_with_no_rest_day(self):
        v = pg.validate_week(_week((8, 8, 8, 8, 8, 8, 8)), WEEK_START)
        self.assertFalse(v.ok)
        self.assertIn("rest day", v.errors[0])

    def test_rejects_non_numeric_duration(self):
        days = _week()
        days[0]["duration_min"] = "about an hour"
        v = pg.validate_week(days, WEEK_START)
        self.assertFalse(v.ok)

    def test_accepts_a_sane_week_unchanged(self):
        v = pg.validate_week(_week(), WEEK_START)
        self.assertTrue(v.ok)
        self.assertEqual(v.notes, [])
        self.assertEqual(len(v.days), 7)
        self.assertEqual(v.summary(), "Applied as proposed.")


class LoadCeilingTests(unittest.TestCase):
    def test_clamps_jump_above_last_week(self):
        # 5 runs of 20 km = 100 km against a 30 km week: way over the 20% ceiling.
        v = pg.validate_week(_week((20, 20, 20, 20, 20, None, None)), WEEK_START, last_week_km=30.0)
        self.assertTrue(v.ok, v.errors)
        self.assertLessEqual(pg.week_running_km(v.days), 30.0 * 1.20 + 0.5)
        self.assertTrue(any("above last week" in n for n in v.notes))

    def test_allows_a_modest_rise(self):
        v = pg.validate_week(_week((8, 8, 8, 8, 10, None, None)), WEEK_START, last_week_km=40.0)
        self.assertTrue(v.ok)
        self.assertFalse(any("above last week" in n for n in v.notes))

    def test_no_week_on_week_ceiling_without_history(self):
        """A first run with no known previous week must not be blocked outright."""
        v = pg.validate_week(_week((12, 12, 12, 12, 12, None, None)), WEEK_START, last_week_km=None)
        self.assertTrue(v.ok)
        self.assertFalse(any("above last week" in n for n in v.notes))

    def test_absolute_ceiling_applies_without_history(self):
        v = pg.validate_week(_week((40, 40, 40, 40, 40, None, None)), WEEK_START)
        self.assertTrue(v.ok)
        self.assertLessEqual(pg.week_running_km(v.days), pg.MAX_WEEKLY_KM + 0.5)

    def test_taper_forbids_any_rise(self):
        taper_week = RACE - timedelta(days=14)
        taper_week -= timedelta(days=taper_week.weekday())
        v = pg.validate_week(
            _week((15, 15, 15, 15, 15, None, None), start=taper_week), taper_week,
            last_week_km=40.0, race_date=RACE,
        )
        self.assertTrue(v.ok, v.errors)
        self.assertLessEqual(pg.week_running_km(v.days), 40.0 + 0.5)
        self.assertTrue(any("taper" in n for n in v.notes))

    def test_clamping_only_ever_reduces(self):
        """A light week must not be scaled up to meet any target."""
        light = _week((3, 3, 3, 3, 3, None, None))
        before = pg.week_running_km(light)
        v = pg.validate_week(light, WEEK_START, last_week_km=60.0)
        self.assertTrue(v.ok)
        self.assertAlmostEqual(pg.week_running_km(v.days), before, places=1)

    def test_caps_an_absurd_single_session(self):
        days = _week()
        days[0]["duration_min"] = 900
        v = pg.validate_week(days, WEEK_START)
        self.assertTrue(v.ok)
        self.assertEqual(v.days[0]["duration_min"], pg.MAX_SESSION_MINUTES)


class LongRunShareTests(unittest.TestCase):
    def test_mild_breach_is_clamped_and_share_actually_holds(self):
        """Clamping must solve against the other runs, not take a share of the old total."""
        # 20 km long run on 10 km of support = 67 percent. Allowance is 15 km.
        v = pg.validate_week(_week((5, 5, 20, None, None, None, None)), WEEK_START, last_week_km=100.0)
        self.assertTrue(v.ok, v.errors)
        total = pg.week_running_km(v.days)
        longest = max(pg._run_km(d) for d in v.days)
        self.assertLessEqual(longest / total, pg.MAX_LONG_RUN_SHARE + 0.01)
        self.assertTrue(any("long run cut" in n for n in v.notes))

    def test_severe_breach_is_rejected_not_rewritten(self):
        """A 30 km long run on 6 km of support is a broken week, not a big number."""
        v = pg.validate_week(_week((2, 2, 2, 30, None, None, None)), WEEK_START, last_week_km=100.0)
        self.assertFalse(v.ok)
        self.assertIn("unsupported", v.errors[0])

    def test_lone_long_run_falls_back_to_the_absolute_cap(self):
        """With no other running the share rule is undefined, so only the hard cap applies.

        A single-run week cannot be repaired by scaling and 25 km is a modest total, so
        rejecting it would be stricter than the risk warrants.
        """
        v = pg.validate_week(_week((25, None, None, None, None, None, None)), WEEK_START)
        self.assertTrue(v.ok, v.errors)
        self.assertEqual(pg._run_km(v.days[0]), 25.0)

        over = pg.validate_week(_week((40, None, None, None, None, None, None)), WEEK_START)
        self.assertTrue(over.ok, over.errors)
        self.assertLessEqual(pg._run_km(over.days[0]), pg.MAX_LONG_RUN_KM)

    def test_hard_cap_on_long_run_distance(self):
        v = pg.validate_week(_week((20, 20, 20, 50, None, None, None)), WEEK_START)
        self.assertTrue(v.ok)
        self.assertLessEqual(max(pg._run_km(d) for d in v.days), pg.MAX_LONG_RUN_KM)

    def test_proportionate_long_run_untouched(self):
        v = pg.validate_week(_week((8, 8, 8, 18, None, None, None)), WEEK_START, last_week_km=60.0)
        self.assertTrue(v.ok)
        self.assertFalse(any("long run cut" in n for n in v.notes))


class MetadataTests(unittest.TestCase):
    def test_missing_distance_counts_as_zero_not_a_crash(self):
        days = _week()
        days[0]["run_details"] = {}
        v = pg.validate_week(days, WEEK_START)
        self.assertTrue(v.ok)

    def test_garbage_distance_is_ignored_safely(self):
        days = _week()
        days[0]["run_details"] = {"distance_km": "about ten"}
        v = pg.validate_week(days, WEEK_START)
        self.assertTrue(v.ok)
        self.assertEqual(pg._run_km(v.days[0]), 0.0)

    def test_input_is_not_mutated(self):
        days = _week((30, 30, 30, 30, 30, None, None))
        original = pg.week_running_km(days)
        pg.validate_week(days, WEEK_START, last_week_km=20.0)
        self.assertAlmostEqual(pg.week_running_km(days), original, places=1)

    def test_summary_reports_rejection_and_adjustment(self):
        rejected = pg.validate_week([], WEEK_START)
        self.assertTrue(rejected.summary().startswith("Rejected:"))
        adjusted = pg.validate_week(_week((30, 30, 30, 30, 30, None, None)), WEEK_START, last_week_km=20.0)
        self.assertTrue(adjusted.summary().startswith("Applied with adjustments:"))


if __name__ == "__main__":
    unittest.main()
