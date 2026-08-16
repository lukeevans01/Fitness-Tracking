"""Tests for the interactive weekly review CLI.

The coach call is stubbed, so no network and no LLM. What matters here is that the CLI
cannot write an unsafe week, cannot write without being asked, and writes the canonical
session shape when it does.

Run from the fitness-emails dir:  python3 -m unittest tests.test_review_week
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
TODAY = date(2026, 8, 23)


def _plan(kms=(8, 8, 8, 8, 12, None, None)) -> list:
    days = []
    for i, km in enumerate(kms):
        iso = (WEEK_START + timedelta(days=i)).isoformat()
        if km is None:
            days.append({"date": iso, "session_type": "Rest Day",
                         "session_kind": "rest", "duration_min": 30})
        else:
            days.append({"date": iso, "session_type": "Easy Run",
                         "session_kind": "run", "duration_min": 50,
                         "run_details": {"distance_km": float(km)}})
    return days


class _FixedNow:
    """Stands in for a tz-aware datetime pinned to TODAY."""

    @staticmethod
    def date() -> date:
        return TODAY

    @staticmethod
    def isoformat(timespec: str = "seconds") -> str:
        return f"{TODAY}T09:00:00+02:00"


class _FixedClock:
    """Replaces review_week.datetime so the CLI sees a fixed today."""

    @staticmethod
    def now(tz=None) -> "_FixedNow":
        return _FixedNow()


def _summary(plan_a=None, plan_b=None, plan_c=None) -> dict:
    def option(label, plan):
        opt = {"label": label, "sessions": "Mon: ...", "rationale": "Because."}
        if plan is not None:
            opt["plan"] = plan
        return opt
    return {
        "week_review": "Solid week.",
        "option_a": option("Continue", plan_a),
        "option_b": option("Lighter", plan_b),
        "option_c": option("Recovery", plan_c),
        "recommendation": "B",
        "recommendation_reason": "Sleep is poor.",
        "coach_note": "",
        "improvements": {"running": "Slow down", "lifting": "Add pulling", "nutrition": "More protein"},
    }


class ReviewWeekTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["FITNESS_DB_PATH"] = str(Path(self._tmp.name) / "app.db")
        os.environ["GEMINI_API_KEY"] = "stub"
        for mod in ("store", "review_week", "send_sunday"):
            sys.modules.pop(mod, None)
        import store
        import review_week
        self.store = store
        self.rw = review_week
        self.pid = "luke"
        # Fixed clock and a known recent-volume history. datetime is immutable, so the
        # module's reference to it is swapped rather than the type patched.
        self._patches = [
            mock.patch.object(review_week, "_recent_weekly_km", return_value=[38.0, 39.0, 40.0]),
            mock.patch.object(review_week, "datetime", _FixedClock),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("FITNESS_DB_PATH", None)
        self._tmp.cleanup()
        for mod in ("store", "review_week", "send_sunday"):
            sys.modules.pop(mod, None)

    def _run(self, argv, summary):
        with mock.patch.object(self.rw.coach_orchestrator, "generate_weekly_summary",
                               return_value=summary):
            return self.rw.main(argv)

    def _overrides(self):
        return self.store.get_overrides(self.pid)

    def test_review_only_writes_nothing(self):
        code = self._run([], _summary(plan_b=_plan()))
        self.assertEqual(code, 0)
        self.assertEqual(self._overrides(), {})

    def test_apply_writes_the_chosen_week(self):
        code = self._run(["--apply", "B"], _summary(plan_b=_plan()))
        self.assertEqual(code, 0)
        overrides = self._overrides()
        self.assertEqual(len(overrides), 7)
        record = overrides[WEEK_START.isoformat()]
        self.assertEqual(record["edit_source"], "weekly_review_cli")
        self.assertNotIn("date", record["session"])

    def test_lowercase_letter_accepted(self):
        self.assertEqual(self._run(["--apply", "b"], _summary(plan_b=_plan())), 0)
        self.assertEqual(len(self._overrides()), 7)

    def test_refuses_to_apply_an_unsafe_week(self):
        unsafe = _plan((2, 2, 2, 30, None, None, None))
        code = self._run(["--apply", "B"], _summary(plan_b=unsafe))
        self.assertEqual(code, 1)
        self.assertEqual(self._overrides(), {})

    def test_refuses_to_apply_an_option_with_no_plan(self):
        code = self._run(["--apply", "A"], _summary(plan_b=_plan()))
        self.assertEqual(code, 1)
        self.assertEqual(self._overrides(), {})

    def test_anchor_not_persisted_by_default(self):
        self._run([], _summary(plan_b=_plan()))
        self.assertNotIn("volume_anchor_km", self.store.get_adaptation(self.pid))

    def test_anchor_persisted_on_request(self):
        self._run(["--recalibrate-anchor"], _summary(plan_b=_plan()))
        adaptation = self.store.get_adaptation(self.pid)
        self.assertIn("volume_anchor_km", adaptation)
        self.assertIn("volume_anchor_week", adaptation)

    def test_coach_failure_exits_nonzero_without_writing(self):
        with mock.patch.object(self.rw.coach_orchestrator, "generate_weekly_summary",
                               side_effect=RuntimeError("gemini down")):
            code = self.rw.main(["--apply", "B"])
        self.assertEqual(code, 1)
        self.assertEqual(self._overrides(), {})

    def test_does_not_touch_the_pending_choice(self):
        """The CLI must not interfere with an A/B/C email reply already in flight."""
        self.store.set_pending_choice(self.pid, {"week_start": WEEK_START.isoformat(),
                                                "options": {}, "chosen": None})
        self._run(["--apply", "B"], _summary(plan_b=_plan()))
        self.assertIsNone(self.store.get_pending_choice(self.pid)["chosen"])


class DefaultWeekStartTests(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("review_week", None)
        import review_week
        self.rw = review_week

    def test_sunday_plans_tomorrow(self):
        self.assertEqual(self.rw._default_week_start(date(2026, 8, 23)), date(2026, 8, 24))

    def test_monday_plans_the_current_week(self):
        self.assertEqual(self.rw._default_week_start(date(2026, 8, 24)), date(2026, 8, 24))

    def test_midweek_plans_the_next_monday(self):
        self.assertEqual(self.rw._default_week_start(date(2026, 8, 26)), date(2026, 8, 31))


if __name__ == "__main__":
    unittest.main()
