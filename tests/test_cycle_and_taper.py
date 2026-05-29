"""Tests for Pack 04 — cycle maths, taper window, and reply-text stripping."""

import sys
import unittest
from datetime import date
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import coach_orchestrator
import process_replies


# ---------------------------------------------------------------------------
# Minimal 7-day plan dict (mirrors plan_template.json structure)
# ---------------------------------------------------------------------------

def _make_plan(start: str = "2026-05-25") -> dict:
    days = [
        {"day_num": i, "session_type": f"Session {i}", "session_kind": "strength",
         "duration_min": 60, "short_version": f"s{i}", "purpose": "base",
         "warm_up": "", "exercises": [],
         "run_details": {"pace": "", "hr_target": "", "duration": "", "distance": "", "effort": ""},
         "extras": ""}
        for i in range(1, 8)
    ]
    return {
        "cycle_start_date": start,
        "cycle_length_days": 7,
        "cycle_days": days,
        "hard_rules": [],
    }


# ---------------------------------------------------------------------------
# Test 1: _cycle_day — day number and wrap
# ---------------------------------------------------------------------------

class TestCycleDay(unittest.TestCase):
    def setUp(self):
        self.plan = _make_plan("2026-05-25")

    def test_day1_on_start_date(self):
        day_num, session, _ = process_replies._cycle_day(date(2026, 5, 25), self.plan)
        self.assertEqual(day_num, 1)
        self.assertEqual(session["day_num"], 1)

    def test_day7_on_sixth_day(self):
        day_num, session, _ = process_replies._cycle_day(date(2026, 5, 31), self.plan)
        self.assertEqual(day_num, 7)
        self.assertEqual(session["day_num"], 7)

    def test_wraps_to_day1_after_day7(self):
        """8th day from start should land on day 1 again."""
        day_num, session, _ = process_replies._cycle_day(date(2026, 6, 1), self.plan)
        self.assertEqual(day_num, 1)

    def test_day_after_is_next_day(self):
        """day_after for day 3 should be day 4."""
        _, session, day_after = process_replies._cycle_day(date(2026, 5, 27), self.plan)
        self.assertEqual(session["day_num"], 3)
        self.assertEqual(day_after["day_num"], 4)

    def test_day_after_wraps_at_end(self):
        """day_after for day 7 (last) should be day 1."""
        _, session, day_after = process_replies._cycle_day(date(2026, 5, 31), self.plan)
        self.assertEqual(session["day_num"], 7)
        self.assertEqual(day_after["day_num"], 1)

    def test_full_cycle_covers_all_days(self):
        """A 7-day span from start should visit every day_num exactly once."""
        seen = set()
        for offset in range(7):
            d = date(2026, 5, 25 + offset)
            # Use timedelta-safe approach
            from datetime import timedelta
            d = date(2026, 5, 25) + timedelta(days=offset)
            day_num, _, _ = process_replies._cycle_day(d, self.plan)
            seen.add(day_num)
        self.assertEqual(seen, set(range(1, 8)))


# ---------------------------------------------------------------------------
# Test 2: coach_orchestrator.days_to_race and is_taper_active
# ---------------------------------------------------------------------------

class TestTaper(unittest.TestCase):
    """Race day is 2026-11-22; taper window is 28 days."""

    def test_days_to_race_positive_before_race(self):
        d = coach_orchestrator.days_to_race(date(2026, 10, 1))
        self.assertGreater(d, 0)

    def test_days_to_race_zero_on_race_day(self):
        self.assertEqual(coach_orchestrator.days_to_race(date(2026, 11, 22)), 0)

    def test_days_to_race_negative_after_race(self):
        self.assertLess(coach_orchestrator.days_to_race(date(2026, 11, 23)), 0)

    def test_taper_inactive_at_29_days_out(self):
        """29 days before race is outside the 28-day window."""
        from datetime import timedelta
        d = date(2026, 11, 22) - timedelta(days=29)  # 2026-10-24
        self.assertFalse(coach_orchestrator.is_taper_active(d))

    def test_taper_active_at_28_days_out(self):
        """28 days before race is the first day of the taper window."""
        from datetime import timedelta
        d = date(2026, 11, 22) - timedelta(days=28)
        self.assertTrue(coach_orchestrator.is_taper_active(d))

    def test_taper_active_at_1_day_out(self):
        self.assertTrue(coach_orchestrator.is_taper_active(date(2026, 11, 21)))

    def test_taper_active_on_race_day(self):
        self.assertTrue(coach_orchestrator.is_taper_active(date(2026, 11, 22)))

    def test_taper_inactive_after_race_day(self):
        self.assertFalse(coach_orchestrator.is_taper_active(date(2026, 11, 23)))


# ---------------------------------------------------------------------------
# Test 3: _strip_quoted_history
# ---------------------------------------------------------------------------

class TestStripQuotedHistory(unittest.TestCase):
    def test_keeps_new_text_only(self):
        raw = (
            "Change tomorrow to a rest day please.\n\n"
            "On Thu, 29 May 2026 at 19:00, Luke's Fitness Bot wrote:\n"
            "> Here is your session for tomorrow...\n"
            "> Day 3 — Push Focus\n"
        )
        stripped = process_replies._strip_quoted_history(raw)
        self.assertEqual(stripped, "Change tomorrow to a rest day please.")
        self.assertNotIn("wrote:", stripped)
        self.assertNotIn(">", stripped)

    def test_gt_prefix_line_stops_parsing(self):
        raw = "food log: oats and eggs\n> Original message here"
        stripped = process_replies._strip_quoted_history(raw)
        self.assertEqual(stripped, "food log: oats and eggs")

    def test_dash_separator_stops_parsing(self):
        raw = "revert\n-----Original Message-----\nSome old content"
        stripped = process_replies._strip_quoted_history(raw)
        self.assertEqual(stripped, "revert")

    def test_plain_message_unchanged(self):
        raw = "How am I doing on protein today?"
        stripped = process_replies._strip_quoted_history(raw)
        self.assertEqual(stripped, raw)

    def test_empty_string(self):
        self.assertEqual(process_replies._strip_quoted_history(""), "")


if __name__ == "__main__":
    unittest.main()
