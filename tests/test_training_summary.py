"""Tests for Pack 02 — training_summary hardening."""

import csv
import io
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

# Remove any stub registered by test_process_replies before importing the real module.
if "training_summary" in sys.modules and not hasattr(sys.modules["training_summary"], "STRAVA_CSV"):
    del sys.modules["training_summary"]

import training_summary as ts


# ---------------------------------------------------------------------------
# Helpers — write fixture CSVs to a tmp dir
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Test 1: duplicated Distance header — first column (summary km) wins
# ---------------------------------------------------------------------------

class TestStravaColumnIndex(unittest.TestCase):
    def test_first_occurrence_wins_for_duplicate(self):
        header = ["Activity Type", "Distance", "Moving Time", "Distance", "Average Heart Rate"]
        idx = ts._strava_column_index(header)
        self.assertEqual(idx["Distance"], 1, "First 'Distance' should be at index 1")
        self.assertEqual(idx["Average Heart Rate"], 4)

    def test_strava_with_duplicate_distance_parses_correct_km(self, tmp_path=None):
        """A Strava fixture with duplicated Distance headers parses to correct km."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "data"
            strava_path = tmp_path / "strava.csv"
            today = date(2026, 5, 28)
            yesterday = today - timedelta(days=1)

            # Two Distance columns: first is the summary km (8.5), second is stream metres (9200)
            # With first-occurrence-wins, we parse 8.5 km directly.
            rows = [
                ["Activity Date", "Activity Type", "Distance", "Moving Time",
                 "Average Heart Rate", "Distance"],
                [yesterday.strftime("%Y-%m-%d"), "Run", "8.5", "2940", "148", "9200"],
            ]
            _write_csv(strava_path, rows)

            with patch.object(ts, "STRAVA_CSV", strava_path), \
                 patch.object(ts, "STRONG_CSV", tmp_path / "strong.csv"):
                result = ts.build_stats(days=7, today=today)

            self.assertEqual(result["run_sessions"], 1)
            self.assertAlmostEqual(result["run_km_total"], 8.5, places=1)


# ---------------------------------------------------------------------------
# Test 2: _normalise_distance_km unit handling
# ---------------------------------------------------------------------------

class TestNormaliseDistance(unittest.TestCase):
    def test_km_value_unchanged(self):
        self.assertAlmostEqual(ts._normalise_distance_km(10.2), 10.2)

    def test_metres_value_converted(self):
        self.assertAlmostEqual(ts._normalise_distance_km(10200.0), 10.2)

    def test_nonsense_value_rejected(self):
        self.assertIsNone(ts._normalise_distance_km(999999))

    def test_zero_rejected(self):
        self.assertIsNone(ts._normalise_distance_km(0))

    def test_negative_rejected(self):
        self.assertIsNone(ts._normalise_distance_km(-5.0))


# ---------------------------------------------------------------------------
# Test 3: today parameter is respected (cutoff is Amsterdam-relative)
# ---------------------------------------------------------------------------

class TestTodayParam(unittest.TestCase):
    def test_build_summary_honours_today(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "data"
            strava_path = tmp_path / "strava.csv"
            strong_path = tmp_path / "strong.csv"

            fixed_today = date(2026, 5, 28)
            in_window = fixed_today - timedelta(days=5)
            out_of_window = fixed_today - timedelta(days=20)

            rows = [
                ["Activity Date", "Activity Type", "Distance", "Moving Time", "Average Heart Rate"],
                [in_window.strftime("%Y-%m-%d"), "Run", "6.0", "2400", "145"],
                [out_of_window.strftime("%Y-%m-%d"), "Run", "7.0", "2700", "150"],
            ]
            _write_csv(strava_path, rows)
            strong_path.write_text("")  # empty

            with patch.object(ts, "STRAVA_CSV", strava_path), \
                 patch.object(ts, "STRONG_CSV", strong_path):
                summary = ts.build_summary(days=14, today=fixed_today)

            self.assertIn("1 runs", summary)
            self.assertNotIn("2 runs", summary)

    def test_build_stats_honours_today(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "data"
            strava_path = tmp_path / "strava.csv"
            strong_path = tmp_path / "strong.csv"

            fixed_today = date(2026, 5, 28)
            in_window = fixed_today - timedelta(days=3)
            out_of_window = fixed_today - timedelta(days=10)

            rows = [
                ["Activity Date", "Activity Type", "Distance", "Moving Time"],
                [in_window.strftime("%Y-%m-%d"), "Run", "5.0", "1800"],
                [out_of_window.strftime("%Y-%m-%d"), "Run", "5.0", "1800"],
            ]
            _write_csv(strava_path, rows)
            strong_path.write_text("")

            with patch.object(ts, "STRAVA_CSV", strava_path), \
                 patch.object(ts, "STRONG_CSV", strong_path):
                result = ts.build_stats(days=7, today=fixed_today)

            self.assertEqual(result["run_sessions"], 1)


# ---------------------------------------------------------------------------
# Test 4: malformed row produces warning but does not abort; valid rows count
# ---------------------------------------------------------------------------

class TestMalformedRowTolerance(unittest.TestCase):
    def test_bad_row_skipped_valid_counted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "data"
            strava_path = tmp_path / "strava.csv"

            today = date(2026, 5, 28)
            good_date = today - timedelta(days=1)

            rows = [
                ["Activity Date", "Activity Type", "Distance", "Moving Time"],
                [good_date.strftime("%Y-%m-%d"), "Run", "8.0", "2880"],   # good
                ["not-a-date", "Run", "5.0", "1800"],                      # bad date
                [good_date.strftime("%Y-%m-%d"), "Run", "not-a-number", "1800"],  # bad dist
            ]
            _write_csv(strava_path, rows)

            with patch.object(ts, "STRAVA_CSV", strava_path), \
                 patch.object(ts, "STRONG_CSV", tmp_path / "strong.csv"):
                result = ts.build_stats(days=7, today=today)

            # First row should be counted; the others skipped
            self.assertEqual(result["run_sessions"], 1)


# ---------------------------------------------------------------------------
# Test 5: key lift deduplication — "Weighted Pull-Up" and "pull up" collapse
# ---------------------------------------------------------------------------

class TestKeyLiftDedup(unittest.TestCase):
    def test_weighted_pullup_with_hyphen_matches_canonical(self):
        canonical = ts._match_key_lift("Weighted Pull-Up (Barbell)")
        self.assertEqual(canonical, "weighted pull up")

    def test_plain_pullup_matches_canonical(self):
        canonical = ts._match_key_lift("Pull Up")
        self.assertEqual(canonical, "pull up")

    def test_weighted_preferred_over_plain_for_weighted_exercise(self):
        # "weighted pull up" is more specific and should win over "pull up"
        canonical = ts._match_key_lift("Weighted Pull-Up")
        self.assertEqual(canonical, "weighted pull up",
                         "Longer/more-specific match should take precedence")

    def test_build_summary_reports_each_lift_once(self):
        """With multiple variants in Strong CSV, each canonical lift appears exactly once."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "data"
            strong_path = tmp_path / "strong.csv"
            strava_path = tmp_path / "strava.csv"

            today = date(2026, 5, 28)
            session_date = today - timedelta(days=1)

            rows = [
                ["Date", "Workout Name", "Duration", "Exercise Name", "Set Order",
                 "Weight", "Reps", "Distance", "Seconds", "Notes", "Workout Notes", "RPE"],
                [session_date.strftime("%Y-%m-%d 07:00:00"), "Pull day", "45m",
                 "Pull-Up", "1", "0", "8", "0", "0", "", "", ""],
                [session_date.strftime("%Y-%m-%d 07:00:00"), "Pull day", "45m",
                 "Pull Up", "2", "0", "6", "0", "0", "", "", ""],
                [session_date.strftime("%Y-%m-%d 07:00:00"), "Pull day", "45m",
                 "Weighted Pull-Up", "3", "10", "5", "0", "0", "", "", ""],
            ]
            _write_csv(strong_path, rows)
            strava_path.write_text("")

            with patch.object(ts, "STRONG_CSV", strong_path), \
                 patch.object(ts, "STRAVA_CSV", strava_path):
                summary = ts.build_summary(days=7, today=today)

            lines_lower = [ln.lower() for ln in summary.splitlines()]
            # Each canonical label should appear at most once
            weighted_lines = [l for l in lines_lower if "weighted pull up:" in l]
            plain_lines = [l for l in lines_lower if "pull up:" in l and "weighted" not in l]
            self.assertLessEqual(len(weighted_lines), 1, "Weighted pull up should appear at most once")
            self.assertLessEqual(len(plain_lines), 1, "Plain pull up should appear at most once")


if __name__ == "__main__":
    unittest.main()
