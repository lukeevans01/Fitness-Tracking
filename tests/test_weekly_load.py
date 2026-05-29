"""Tests for Pack 09 — weekly_load.WeeklyLoad cross-domain model.

Load computation is deterministic (no LLM). Strava/Strong CSVs are written to a temp dir
and patched in; nutrition comes from a temp SQLite store via FITNESS_DB_PATH. The
generate_session injection test patches gemini_client.call_gemini to capture the prompt.

Run from the fitness-emails dir:  python3 -m unittest tests.test_weekly_load
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

# Drop any stubs left by test_process_replies before importing the real modules.
for _name in ("training_summary", "weekly_load"):
    mod = sys.modules.get(_name)
    if mod is not None and not hasattr(mod, "__file__"):
        del sys.modules[_name]

import training_summary as ts  # noqa: E402
import weekly_load  # noqa: E402
import store  # noqa: E402
import coach_orchestrator  # noqa: E402
from weekly_load import WeeklyLoad  # noqa: E402


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


class BuildWeeklyLoadTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._strava = tmp / "strava.csv"
        self._strong = tmp / "strong.csv"
        self._db = tmp / "app.db"
        self._prev_db = os.environ.get("FITNESS_DB_PATH")
        os.environ["FITNESS_DB_PATH"] = str(self._db)
        self.today = date(2026, 5, 28)

        # One quality run (yesterday, 10 km in 48 min => 4.8 min/km < 5:06), one easy run
        # (4 days ago, 8 km in 56 min => 7.0 min/km), one squash activity (3 days ago).
        yesterday = self.today - timedelta(days=1)
        easy_day = self.today - timedelta(days=4)
        squash_day = self.today - timedelta(days=3)
        _write_csv(self._strava, [
            ["Activity Date", "Activity Type", "Distance", "Moving Time", "Average Heart Rate"],
            [yesterday.strftime("%Y-%m-%d"), "Run", "10.0", "2880", "165"],
            [easy_day.strftime("%Y-%m-%d"), "Run", "8.0", "3360", "140"],
            [squash_day.strftime("%Y-%m-%d"), "Squash", "0", "3600", "150"],
        ])
        # One strength session 2 days ago: Back Squat 100kg x 5, x 5 (key lift) + an accessory.
        strength_day = (self.today - timedelta(days=2)).strftime("%Y-%m-%d") + " 07:15:00"
        _write_csv(self._strong, [
            ["Date", "Workout Name", "Duration", "Exercise Name", "Set Order",
             "Weight", "Reps", "Distance", "Seconds", "Notes", "Workout Notes", "RPE"],
            [strength_day, "Legs", "1h", "Back Squat", "1", "100.0", "5.0", "0", "0", "", "", ""],
            [strength_day, "Legs", "1h", "Back Squat", "2", "100.0", "5.0", "0", "0", "", "", ""],
            [strength_day, "Legs", "1h", "Bicep Curl", "1", "20.0", "12.0", "0", "0", "", "", ""],
        ])

        store.append_nutrition("luke", self.today.isoformat(),
                               [{"protein_g": 140, "carbs_g": 300, "fat_g": 70, "kcal": 2400}])
        store.append_nutrition("luke", (self.today - timedelta(days=1)).isoformat(),
                               [{"protein_g": 120, "carbs_g": 250, "fat_g": 60, "kcal": 2000}])

    def tearDown(self):
        if self._prev_db is None:
            os.environ.pop("FITNESS_DB_PATH", None)
        else:
            os.environ["FITNESS_DB_PATH"] = self._prev_db
        self._tmp.cleanup()

    def _build(self):
        with patch.object(ts, "STRAVA_CSV", self._strava), \
             patch.object(ts, "STRONG_CSV", self._strong):
            return weekly_load.build_weekly_load(days=7, today=self.today, profile_id="luke")

    def test_run_and_squash_counts(self):
        load = self._build()
        self.assertEqual(load.run_sessions, 2)
        self.assertAlmostEqual(load.run_km, 18.0, places=1)
        self.assertEqual(load.squash_sessions, 1)

    def test_strength_sessions_and_tonnage_key_lift_only(self):
        load = self._build()
        self.assertEqual(load.strength_sessions, 1)
        # Only the Back Squat sets count: 100*5 + 100*5 = 1000. The accessory is excluded.
        self.assertAlmostEqual(load.strength_tonnage, 1000.0, places=1)

    def test_days_since_last_hard_uses_most_recent(self):
        load = self._build()
        # Quality run was yesterday (1 day ago); key lift 2 days ago. Most recent => 1.
        self.assertEqual(load.days_since_last_hard, 1)

    def test_nutrition_averages_from_store(self):
        load = self._build()
        self.assertEqual(load.days_logged, 2)
        self.assertAlmostEqual(load.avg_protein_g, 130.0, places=1)
        self.assertAlmostEqual(load.avg_kcal, 2200.0, places=1)


class PromptBlockTests(unittest.TestCase):

    def test_block_contains_key_figures(self):
        load = WeeklyLoad(
            run_sessions=4, run_km=38.5, strength_sessions=2, strength_tonnage=12400.0,
            squash_sessions=1, days_since_last_hard=3, avg_protein_g=145.0,
            avg_kcal=2450.0, days_logged=5,
        )
        block = load.to_prompt_block()
        self.assertIn("WEEKLY LOAD", block)
        self.assertIn("38.5 km", block)
        self.assertIn("12,400 kg", block)
        self.assertIn("Squash: 1", block)
        self.assertIn("Days since last hard session: 3", block)
        self.assertIn("5/7 days logged", block)

    def test_recent_hard_rule_fires(self):
        load = WeeklyLoad(
            run_sessions=3, run_km=20.0, strength_sessions=1, strength_tonnage=800.0,
            squash_sessions=0, days_since_last_hard=1, avg_protein_g=150.0,
            avg_kcal=2600.0, days_logged=4,
        )
        block = load.to_prompt_block()
        self.assertIn("Hard rules derived", block)
        self.assertIn("last 48 hours", block)

    def test_fuelling_deficit_rule_fires(self):
        load = WeeklyLoad(
            run_sessions=2, run_km=15.0, strength_sessions=1, strength_tonnage=500.0,
            squash_sessions=0, days_since_last_hard=5, avg_protein_g=110.0,
            avg_kcal=1900.0, days_logged=3,
        )
        block = load.to_prompt_block()
        self.assertIn("fuelling looks low", block)

    def test_no_rules_when_load_is_benign(self):
        load = WeeklyLoad(
            run_sessions=2, run_km=15.0, strength_sessions=1, strength_tonnage=500.0,
            squash_sessions=0, days_since_last_hard=5, avg_protein_g=150.0,
            avg_kcal=2700.0, days_logged=4,
        )
        block = load.to_prompt_block()
        self.assertNotIn("Hard rules derived", block)


class GenerateSessionInjectionTests(unittest.TestCase):

    def test_weekly_load_block_injected_into_prompt(self):
        load = WeeklyLoad(
            run_sessions=4, run_km=38.5, strength_sessions=2, strength_tonnage=12400.0,
            squash_sessions=1, days_since_last_hard=1, avg_protein_g=145.0,
            avg_kcal=2450.0, days_logged=5,
        )
        valid_session = json.dumps({
            "session_type": "Easy run",
            "session_kind": "run",
            "duration_min": 40,
            "short_version": "20 min jog",
            "purpose": "aerobic base",
            "coach_note": "Kept it easy.",
        })
        current_session = {"session_type": "Easy run", "session_kind": "run", "duration_min": 40}

        captured = {}

        def fake_call(prompt):
            captured["prompt"] = prompt
            return valid_session

        with patch.object(coach_orchestrator.gemini_client, "call_gemini", side_effect=fake_call):
            coach_orchestrator.generate_session(
                domain="run",
                reply_text="how's my load?",
                current_session=current_session,
                training_summary="",
                weekly_load=load,
            )

        self.assertIn("WEEKLY LOAD", captured["prompt"])
        self.assertIn("last 48 hours", captured["prompt"])  # recent-hard rule travels in


if __name__ == "__main__":
    unittest.main()
