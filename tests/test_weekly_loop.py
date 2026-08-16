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
        for mod in ("store", "plan_writer", "process_replies"):
            sys.modules.pop(mod, None)
        import store
        import plan_writer
        import process_replies
        self.store = store
        self.pw = plan_writer
        self.pr = process_replies
        self.pid = process_replies._active_profile_id()
        # No Strava history in the temp env, so the week-on-week ceiling is skipped.
        # Patched on plan_writer, which now owns the shared validate-and-write step.
        self._patch = mock.patch.object(plan_writer, "last_week_running_km", return_value=None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        os.environ.pop("FITNESS_DB_PATH", None)
        self._tmp.cleanup()
        for mod in ("store", "plan_writer", "process_replies"):
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
        # Distinct per path: reply vs cli vs the Sunday auto-apply.
        self.assertEqual(record["edit_source"], "weekly_review_reply")
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
        with mock.patch.object(self.pw, "last_week_running_km", return_value=20.0):
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


class AutoApplyTests(unittest.TestCase):
    """The Sunday review applies its own recommendation, unattended."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["FITNESS_DB_PATH"] = str(Path(self._tmp.name) / "app.db")
        for mod in ("store", "plan_writer", "send_sunday"):
            sys.modules.pop(mod, None)
        import store
        import plan_writer
        import send_sunday
        self.store = store
        self.pw = plan_writer
        self.ss = send_sunday
        self.profile = send_sunday.default_profile()
        self._patch = mock.patch.object(plan_writer, "last_week_running_km", return_value=None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        os.environ.pop("FITNESS_DB_PATH", None)
        self._tmp.cleanup()
        for mod in ("store", "plan_writer", "send_sunday"):
            sys.modules.pop(mod, None)

    def _summary(self, recommendation="B", plan=None):
        def option(label):
            opt = {"label": label, "sessions": "Mon: ...", "rationale": "Because."}
            return opt
        summary = {
            "option_a": option("Continue"),
            "option_b": option("Lighter"),
            "option_c": option("Recovery"),
            "recommendation": recommendation,
        }
        if plan is not None:
            summary[f"option_{recommendation.lower()}"]["plan"] = plan
        return summary

    def test_recommended_option_is_written_without_a_reply(self):
        note = self.ss._auto_apply_recommended(
            self._summary("B", _plan()), WEEK_START, TODAY, self.profile)
        overrides = self.store.get_overrides(self.profile.id)
        self.assertEqual(len(overrides), 7)
        self.assertEqual(overrides[WEEK_START.isoformat()]["edit_source"], "weekly_review_auto")
        self.assertIn("has been applied", note)
        self.assertIn("Reply A, B or C", note)

    def test_unsafe_recommendation_is_not_written(self):
        unsafe = _plan((2, 2, 2, 30, None, None, None))
        note = self.ss._auto_apply_recommended(
            self._summary("B", unsafe), WEEK_START, TODAY, self.profile)
        self.assertEqual(self.store.get_overrides(self.profile.id), {})
        self.assertIn("not applied", note)

    def test_recommendation_without_a_plan_leaves_the_template(self):
        note = self.ss._auto_apply_recommended(
            self._summary("B", None), WEEK_START, TODAY, self.profile)
        self.assertEqual(self.store.get_overrides(self.profile.id), {})
        self.assertIn("standard cycle stands", note)

    def test_garbage_recommendation_is_handled(self):
        for bad in ("D", "", None):
            note = self.ss._auto_apply_recommended(
                {"recommendation": bad}, WEEK_START, TODAY, self.profile)
            self.assertIn("No valid recommendation", note)
        self.assertEqual(self.store.get_overrides(self.profile.id), {})

    def test_a_later_reply_overwrites_the_auto_applied_week(self):
        """Auto-apply sets the default; replying must still switch options."""
        self.ss._auto_apply_recommended(self._summary("B", _plan()), WEEK_START, TODAY, self.profile)
        first = self.store.get_overrides(self.profile.id)[WEEK_START.isoformat()]
        self.assertEqual(first["session"]["session_type"], "Easy Run")

        swapped = _plan()
        for day in swapped:
            if day["session_kind"] == "run":
                day["session_type"] = "Tempo Run"
        _, _, _ = self.pw.apply_week(
            self.profile.id, swapped, WEEK_START,
            today=TODAY, source="weekly_review_reply", race_date=self.profile.race_date,
        )
        after = self.store.get_overrides(self.profile.id)[WEEK_START.isoformat()]
        self.assertEqual(after["session"]["session_type"], "Tempo Run")
        self.assertEqual(after["edit_source"], "weekly_review_reply")
