"""Tests for the Sunday review's timing and its measurement window.

The review moved from 08:00 to 18:00 Amsterdam so it sees a finished week, including
Sunday's long run. Moving the clock alone would have changed nothing, because the window
skipped the current week regardless, so the two belong together and are pinned together
here.

No network. The activity reader is stubbed so the window boundaries are exact.

Run from the fitness-emails dir:  python3 -m unittest tests.test_sunday_schedule
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import send_sunday  # noqa: E402


class _Activity:
    def __init__(self, day: date, km: float, kind: str = "run"):
        self.date = day
        self.distance_km = km
        self.kind = kind


# One 10 km run on the Monday of each of five consecutive weeks.
WEEK_MONDAYS = [date(2026, 7, 20), date(2026, 7, 27), date(2026, 8, 3),
                date(2026, 8, 10), date(2026, 8, 17)]
ACTIVITIES = [_Activity(m, float(10 + i)) for i, m in enumerate(WEEK_MONDAYS)]


class WindowTests(unittest.TestCase):
    def setUp(self):
        # Patch the reader the function resolves at call time.
        self._patch = mock.patch.dict(
            "sys.modules",
            {"ingest": mock.Mock(get_reader=lambda kind: (lambda path: ACTIVITIES))},
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_sunday_includes_the_week_just_finished(self):
        """The whole point of the 18:00 move: Sunday's own week must count."""
        totals = send_sunday._recent_weekly_km(date(2026, 8, 16))
        self.assertEqual(totals, [11.0, 12.0, 13.0])

    def test_midweek_excludes_the_week_in_progress(self):
        """A part-finished week would understate the load, so it is left out."""
        totals = send_sunday._recent_weekly_km(date(2026, 8, 19))
        self.assertEqual(totals, [11.0, 12.0, 13.0])
        self.assertNotIn(14.0, totals)

    def test_window_length_is_respected(self):
        self.assertEqual(len(send_sunday._recent_weekly_km(date(2026, 8, 16), weeks=2)), 2)
        self.assertEqual(len(send_sunday._recent_weekly_km(date(2026, 8, 16), weeks=4)), 4)

    def test_weeks_are_oldest_first(self):
        totals = send_sunday._recent_weekly_km(date(2026, 8, 16))
        self.assertEqual(totals, sorted(totals))

    def test_a_week_with_no_running_counts_as_zero(self):
        """A lay-off has to show up in the anchor rather than being averaged away."""
        with mock.patch.dict(
            "sys.modules",
            {"ingest": mock.Mock(get_reader=lambda kind: (lambda path: [
                _Activity(date(2026, 8, 10), 20.0)]))},
        ):
            totals = send_sunday._recent_weekly_km(date(2026, 8, 16))
        self.assertEqual(totals, [0.0, 0.0, 20.0])

    def test_non_runs_are_ignored(self):
        with mock.patch.dict(
            "sys.modules",
            {"ingest": mock.Mock(get_reader=lambda kind: (lambda path: [
                _Activity(date(2026, 8, 10), 20.0, kind="ride")]))},
        ):
            self.assertEqual(send_sunday._recent_weekly_km(date(2026, 8, 16)), [0.0, 0.0, 0.0])

    def test_unreadable_source_returns_empty_not_a_crash(self):
        with mock.patch.dict(
            "sys.modules",
            {"ingest": mock.Mock(get_reader=mock.Mock(side_effect=OSError("no file")))},
        ):
            self.assertEqual(send_sunday._recent_weekly_km(date(2026, 8, 16)), [])


class TimeGateTests(unittest.TestCase):
    """The gate must accept 18:00 Amsterdam and reject the old 08:00 slot."""

    def _run_at(self, when: date, hour: int, minute: int = 0, weekday_override=None):
        fake_now = mock.Mock()
        fake_now.weekday.return_value = weekday_override if weekday_override is not None else when.weekday()
        fake_now.hour = hour
        fake_now.minute = minute
        fake_now.strftime = lambda fmt: f"{hour:02d}:{minute:02d}"
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(send_sunday, "datetime") as dt:
            dt.now.return_value = fake_now
            try:
                send_sunday.check_local_time_window()
                return "sent"
            except SystemExit:
                return "skipped"

    SUNDAY = date(2026, 8, 16)

    def test_accepts_18_00(self):
        self.assertEqual(self._run_at(self.SUNDAY, 18, 0), "sent")

    def test_accepts_within_half_an_hour(self):
        self.assertEqual(self._run_at(self.SUNDAY, 17, 35), "sent")
        self.assertEqual(self._run_at(self.SUNDAY, 18, 25), "sent")

    def test_rejects_the_old_morning_slot(self):
        self.assertEqual(self._run_at(self.SUNDAY, 8, 0), "skipped")

    def test_rejects_well_outside_the_window(self):
        self.assertEqual(self._run_at(self.SUNDAY, 12, 0), "skipped")
        self.assertEqual(self._run_at(self.SUNDAY, 22, 0), "skipped")

    def test_rejects_any_day_but_sunday(self):
        self.assertEqual(self._run_at(self.SUNDAY, 18, 0, weekday_override=2), "skipped")


class CronTests(unittest.TestCase):
    def test_workflow_cron_matches_18_00_amsterdam(self):
        """16:00 UTC covers CEST and 17:00 UTC covers CET; the script gates the rest."""
        text = (FITNESS_DIR / ".github/workflows/sunday-reminder.yml").read_text(encoding="utf-8")
        self.assertIn("cron: '0 16,17 * * 0'", text)
        self.assertNotIn("cron: '0 6,7 * * 0'", text)


if __name__ == "__main__":
    unittest.main()
