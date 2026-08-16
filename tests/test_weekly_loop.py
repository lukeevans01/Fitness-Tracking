"""Tests that the weekly review actually changes the plan.

Before this loop closed, replying "B" stored the chosen sessions as prose and every daily
email still rendered the untouched template. These tests pin the behaviour that matters:
a confirmed choice writes per-date overrides, an unsafe one does not, and a past day is
never rewritten.

Runs against a temp database via FITNESS_DB_PATH; no network and no LLM.

Run from the fitness-emails dir:  python3 -m unittest tests.test_weekly_loop
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

WEEK_START = date(2026, 8, 24)
TODAY = date(2026, 8, 23)   # the Sunday the review is sent


def _plan(kms=(8, 8, 8, 8, 12, None, None), start: date = WEEK_START) -> list:
    days = []
    for i, km in enumerate(kms):
        iso = (start + timedelta(days=i)).isoformat()
        if km is None:
            days.append({"date": iso, "session_type": "Rest Day",
                         "session_kind": "rest", "duration_min": 30})
        else:
            days.append({"date": iso, "session_type": "Easy Run",
                         "session_kind": "run", "duration_min": 50,
                         "run_details": {"distance_km": float(km)},
                         "details": "Keep it easy.", "purpose": "Aerobic base"})
    return days


def _pending(plan=None, letter="B") -> dict:
    option = {
        "label": "Slightly lighter week",
        "sessions": "Mon: Easy run\nTue: Easy run\n...",
        "rationale": "Sleep has been poor.",
    }
    if plan is not None:
        option["plan"] = plan
    return {
        "week_label": "24-30 Aug",
        "week_start": WEEK_START.isoformat(),
        "expires": (WEEK_START + timedelta(days=7)).isoformat(),
        "options": {letter: option},
        "recommendation": letter,
        "chosen": None,
    }


class WeeklyLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["FITNESS_DB_PATH"] = str(Path(self._tmp.name) / "app.db")
        for mod in ("store", "process_replies"):
            sys.modules.pop(mod, None)
        import store
        import process_replies
        self.store = store
        self.pr = process_replies
        self.pid = process_replies._active_profile_id()
        # No Strava history in the temp env, so the week-on-week ceiling is skipped.
        self._patch = mock.patch.object(process_replies, "_last_week_running_km", return_value=None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        os.environ.pop("FITNESS_DB_PATH", None)
        self._tmp.cleanup()
        for mod in ("store", "process_replies"):
            sys.modules.pop(mod, None)

    def _overrides(self):
        return self.store.get_overrides(self.pid)

    def test_choice_writes_overrides_for_the_week(self):
        self.store.set_pending_choice(self.pid, _pending(_plan()))
        state = {}
        result = self.pr._apply_week_choice("B", state, TODAY)

        self.assertIsNotNone(result)
        overrides = self._overrides()
        self.assertEqual(len(overrides), 7, overrides.keys())
        for i in range(7):
            self.assertIn((WEEK_START + timedelta(days=i)).isoformat(), overrides)

    def test_written_sessions_are_the_canonical_shape(self):
        self.store.set_pending_choice(self.pid, _pending(_plan()))
        self.pr._apply_week_choice("B", {}, TODAY)

        record = self._overrides()[WEEK_START.isoformat()]
        self.assertEqual(record["edit_source"], "weekly_review")
        self.assertIn("applied_at", record)
        session = record["session"]
        self.assertEqual(session["session_kind"], "run")
        self.assertEqual(session["session_type"], "Easy Run")
        # The date is the store key, not part of the session body.
        self.assertNotIn("date", session)

    def test_daily_email_would_render_the_override(self):
        """The whole point: the sender must pick the written session up."""
        self.store.set_pending_choice(self.pid, _pending(_plan()))
        self.pr._apply_week_choice("B", {}, TODAY)

        plan_template = self.pr._load_json(FITNESS_DIR / "plan_template.json")
        overrides = {"overrides": self._overrides()}
        session, _ = self.pr._get_current_session(WEEK_START, plan_template, overrides)
        self.assertEqual(session["session_type"], "Easy Run")

    def test_unsafe_week_is_not_written(self):
        # A 30 km long run on 6 km of other running is rejected by the guardrails.
        self.store.set_pending_choice(self.pid, _pending(_plan((2, 2, 2, 30, None, None, None))))
        result = self.pr._apply_week_choice("B", {}, TODAY)

        self.assertIn("did not pass the safety checks", result)
        self.assertEqual(self._overrides(), {})

    def test_clamped_week_is_written_and_reported(self):
        with mock.patch.object(self.pr, "_last_week_running_km", return_value=20.0):
            self.store.set_pending_choice(self.pid, _pending(_plan((20, 20, 20, 20, 20, None, None))))
            result = self.pr._apply_week_choice("B", {}, TODAY)

        self.assertIn("Adjusted for safety", result)
        overrides = self._overrides()
        self.assertEqual(len(overrides), 7)
        total = sum(
            (r["session"].get("run_details") or {}).get("distance_km", 0)
            for r in overrides.values()
        )
        self.assertLessEqual(total, 20.0 * 1.20 + 0.5)

    def test_option_without_a_plan_leaves_the_template_alone(self):
        """Prose-only options must still confirm, just without changing sessions."""
        self.store.set_pending_choice(self.pid, _pending(plan=None))
        result = self.pr._apply_week_choice("B", {}, TODAY)

        self.assertIn("standard cycle stands", result)
        self.assertEqual(self._overrides(), {})

    def test_past_days_are_never_rewritten(self):
        # Confirming late in the week must not rewrite days already trained.
        midweek = WEEK_START + timedelta(days=3)
        self.store.set_pending_choice(self.pid, _pending(_plan()))
        self.pr._apply_week_choice("B", {}, midweek)

        written = sorted(self._overrides())
        self.assertTrue(all(iso >= midweek.isoformat() for iso in written), written)
        self.assertEqual(len(written), 4)

    def test_expired_choice_is_ignored(self):
        pending = _pending(_plan())
        pending["expires"] = "2026-01-01"
        self.store.set_pending_choice(self.pid, pending)
        self.assertIsNone(self.pr._apply_week_choice("B", {}, TODAY))
        self.assertEqual(self._overrides(), {})

    def test_unknown_letter_is_ignored(self):
        self.store.set_pending_choice(self.pid, _pending(_plan(), letter="A"))
        self.assertIsNone(self.pr._apply_week_choice("C", {}, TODAY))
        self.assertEqual(self._overrides(), {})

    def test_choice_is_recorded_in_state_and_pending(self):
        self.store.set_pending_choice(self.pid, _pending(_plan()))
        state = {}
        self.pr._apply_week_choice("B", state, TODAY)
        self.assertIn("Option B", state["week_choice_label"])
        self.assertEqual(self.store.get_pending_choice(self.pid)["chosen"], "B")


if __name__ == "__main__":
    unittest.main()
