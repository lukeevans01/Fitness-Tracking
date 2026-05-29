"""Tests for routine_selector — deterministic strength-session selection.

Covers muscle classification, the recent-load window, overlap scoring (the selector
avoids what was trained recently), the rendered session schema, and graceful fallback
when no templates exist. No Gemini, no network.

Run from the fitness-emails dir:  python3 -m unittest tests.test_routine_selector
"""

import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import routine_library as rl  # noqa: E402
import routine_selector as rs  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _template(name: str, rows: list[tuple]) -> rl.RoutineTemplate:
    exs = tuple(rl.RoutineExercise(n, k, s, r, "") for n, k, s, r in rows)
    week, slot = rl._template_meta(Path(name + ".csv"))
    return rl.RoutineTemplate(week=week, slot=slot, exercises=exs)


def _write_strong(path: Path, rows: list[tuple]) -> None:
    """rows = (date_str, exercise, weight, reps)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Exercise Name", "Weight", "Reps"])
        for d, ex, wt, reps in rows:
            w.writerow([f"{d} 07:00:00", ex, wt, reps])


class ClassifyTests(unittest.TestCase):
    def test_known_movements(self):
        self.assertEqual(rs.classify_muscles("Back Squat"), {"quads", "glutes"})
        self.assertEqual(rs.classify_muscles("Romanian deadlift"), {"hamstrings", "glutes"})
        self.assertIn("chest", rs.classify_muscles("Incline DB bench press"))
        self.assertIn("back", rs.classify_muscles("Lat pulldown"))
        self.assertIn("core", rs.classify_muscles("Hanging knee raise"))

    def test_unknown_returns_empty(self):
        self.assertEqual(rs.classify_muscles("Mystery machine thing"), set())


class RecentLoadTests(unittest.TestCase):
    def test_window_excludes_old_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strong.csv"
            _write_strong(path, [
                ("2026-05-28", "Back Squat", "100", "5"),   # in window
                ("2026-05-01", "Back Squat", "100", "5"),   # too old
            ])
            load = rs.recent_muscle_load(date(2026, 5, 29), window_days=10, strong_path=path)
        self.assertEqual(load.get("quads"), 1.0)  # only the recent set counted

    def test_missing_csv_is_empty(self):
        self.assertEqual(rs.recent_muscle_load(date(2026, 5, 29), strong_path=Path("/no/such.csv")), {})


class SelectionTests(unittest.TestCase):
    def test_picks_template_that_avoids_recent_work(self):
        # Two templates: one quad-heavy, one back/chest-heavy.
        legs = _template("Week 1 Routine A", [("Back Squat", "Compound", 5, 5)])
        upper = _template("Week 1 Routine B", [("Lat pulldown", "Compound", 4, 10),
                                               ("DB bench press", "Compound", 4, 10)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strong.csv"
            # Recently trained legs hard -> selector should avoid the quad template.
            _write_strong(path, [("2026-05-28", "Back Squat", "100", "5")])
            chosen = rs.select_template(date(2026, 5, 29), templates=[legs, upper],
                                        strong_path=path)
        self.assertEqual(chosen.name, "Week 1 Routine B")

    def test_cold_start_is_stable_first_template(self):
        a = _template("Week 1 Routine A", [("Back Squat", "Compound", 4, 8)])
        b = _template("Week 1 Routine B", [("Bench", "Compound", 4, 8)])
        chosen = rs.select_template(date(2026, 5, 29), templates=[a, b],
                                    strong_path=Path("/no/such.csv"))
        self.assertEqual(chosen.name, "Week 1 Routine A")  # stable tie-break

    def test_select_session_none_when_no_templates(self):
        self.assertIsNone(rs.select_session(date(2026, 5, 29), templates=[]))


class RenderTests(unittest.TestCase):
    def test_session_schema_and_fallback_weight(self):
        t = _template("Week 1 Routine A", [
            ("Back Squat", "Compound", 4, 8),
            ("Pull-up", "Compound", 3, 8),
            ("Standing calf raise", "Isolation", 3, 15),
        ])
        s = rs.routine_to_session(t)
        for key in ("session_type", "session_kind", "duration_min", "warm_up",
                    "exercises", "short_version", "purpose"):
            self.assertIn(key, s)
        self.assertEqual(s["session_kind"], "strength")
        self.assertLessEqual(s["duration_min"], 75)
        names = [e["name"] for e in s["exercises"]]
        self.assertEqual(names, ["Back Squat", "Pull-up", "Standing calf raise"])
        # Anchored barbell weight vs bodyweight vs RIR-3 fallback.
        weights = {e["name"]: e["weight"] for e in s["exercises"]}
        self.assertEqual(weights["Back Squat"], "~85 kg")
        self.assertEqual(weights["Pull-up"], "Bodyweight")
        self.assertEqual(weights["Standing calf raise"], "RIR 3")
        # Each exercise has the four render keys.
        for e in s["exercises"]:
            self.assertEqual(set(e), {"name", "sets_reps", "weight", "rest"})


class RealTemplatesTests(unittest.TestCase):
    def test_select_session_on_deployed_library(self):
        s = rs.select_session(date(2026, 5, 29))
        self.assertIsNotNone(s)
        self.assertEqual(s["session_kind"], "strength")
        self.assertTrue(s["exercises"])


if __name__ == "__main__":
    unittest.main()
