"""Tests for Pack 12 — source-agnostic ingestion adapters.

Covers the Strava and Strong CSV adapters (normalised Activity/LiftSet records) and a
golden-output parity check that build_summary produces the same RUNS block when fed via
the adapters as the pre-refactor implementation did on the real data/strava.csv.
Run from the fitness-emails dir:  python -m unittest tests.test_ingest
"""

import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

# Drop any stub registered by test_process_replies before importing the real module.
if "training_summary" in sys.modules and not hasattr(sys.modules["training_summary"], "STRAVA_CSV"):
    del sys.modules["training_summary"]

import training_summary as ts  # noqa: E402
from ingest import strava_csv, strong_csv  # noqa: E402


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


class StravaAdapterTests(unittest.TestCase):

    def test_duplicate_headers_and_metres_distance(self):
        """Duplicated Distance headers (first wins) and a metres distance both normalise."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strava.csv"
            rows = [
                # Two Distance columns: first is summary km, second is stream metres.
                ["Activity Date", "Activity Type", "Distance", "Moving Time",
                 "Average Heart Rate", "Distance"],
                ["2026-05-20", "Run", "8.5", "2940", "148", "9200"],   # km in first col
                ["2026-05-21", "Run", "9200", "3000", "150", "9200"],  # metres in first col
            ]
            _write_csv(path, rows)

            activities = strava_csv.read_activities(path)

        self.assertEqual(len(activities), 2)
        a0, a1 = activities
        self.assertEqual(a0.kind, "run")
        self.assertEqual(a0.date, date(2026, 5, 20))
        self.assertAlmostEqual(a0.distance_km, 8.5, places=2)
        self.assertEqual(a0.avg_hr, 148.0)
        # Derived pace = (moving_s / 60) / distance_km
        self.assertAlmostEqual(a0.pace_min_km, (2940 / 60) / 8.5, places=4)
        # Second row's first Distance column is metres -> converted to km.
        self.assertAlmostEqual(a1.distance_km, 9.2, places=2)

    def test_non_run_kinds_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strava.csv"
            rows = [
                ["Activity Date", "Activity Type", "Distance", "Moving Time"],
                ["2026-05-20", "Ride", "30.0", "3600"],
                ["2026-05-21", "Swim", "1.5", "1800"],
                ["2026-05-22", "Squash", "0", "2400"],
            ]
            _write_csv(path, rows)
            kinds = [a.kind for a in strava_csv.read_activities(path)]
        self.assertEqual(kinds, ["ride", "swim", "other"])

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            strava_csv.read_activities(Path("/no/such/strava.csv"))


class StrongAdapterTests(unittest.TestCase):

    def test_canonicalises_pullup_variants_to_one_label(self):
        """'Pull-Up' and 'Pull Up' canonicalise to the single label 'pull up'."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strong.csv"
            rows = [
                ["Date", "Workout Name", "Exercise Name", "Weight", "Reps"],
                ["2026-05-20 07:00:00", "Pull day", "Pull-Up", "0", "8"],
                ["2026-05-20 07:00:00", "Pull day", "Pull Up", "5", "6"],
            ]
            _write_csv(path, rows)
            lifts = strong_csv.read_lifts(path)

        self.assertEqual(len(lifts), 2)
        self.assertEqual({lift.exercise for lift in lifts}, {"pull up"})

    def test_non_key_lift_lowercased_and_hyphen_normalised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strong.csv"
            rows = [
                ["Date", "Exercise Name", "Weight", "Reps"],
                ["2026-05-20 07:00:00", "Decline Push-Up", "0", "12"],
            ]
            _write_csv(path, rows)
            lifts = strong_csv.read_lifts(path)
        self.assertEqual(lifts[0].exercise, "decline push up")


class BuildSummaryGoldenParityTests(unittest.TestCase):
    """build_summary on the real data/strava.csv for a fixed window matches the
    pre-refactor (pack 02) output exactly."""

    def test_runs_block_matches_golden(self):
        summary = ts.build_summary(days=14, today=date(2026, 5, 28))
        expected_runs = [
            "RUNS (last 14 days): 1 runs, 16.3 km total",
            "  Pace distribution -- easy (>=6:00/km): 0, moderate (5:06-5:59/km): 1, "
            "quality (<5:06/km): 0",
            "  Longest run: 16.32 km on 2026-05-24 at 5.57 min/km",
            "  Avg HR across runs with data: 153 bpm",
        ]
        for line in expected_runs:
            self.assertIn(line, summary)


if __name__ == "__main__":
    unittest.main()
