"""Tests for routine_library — parsing, prompt rendering, and save round-trip.

Run from the fitness-emails dir:  python -m unittest tests.test_routine_library
"""

import sys
import tempfile
import unittest
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import routine_library as rl  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ReadTemplateTests(unittest.TestCase):

    def test_parses_name_meta_and_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Week 1 Routine A.csv"
            _write(path,
                "Exercise,Type,Sets,Reps,Notes\n"
                "Back squat,Compound,4,8,\n"
                "Pallof press,Core,3,10,/side\n")
            t = rl.read_template(path)

        self.assertEqual(t.week, 1)
        self.assertEqual(t.slot, "A")
        self.assertEqual(t.name, "Week 1 Routine A")
        self.assertEqual(len(t.exercises), 2)
        self.assertEqual(t.exercises[0].name, "Back squat")
        self.assertEqual(t.exercises[0].sets, 4)
        self.assertEqual(t.exercises[0].reps, 8)
        self.assertEqual(t.exercises[1].notes, "/side")

    def test_blank_rows_skipped_and_primary_is_first_compound(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Week 2 Routine C.csv"
            _write(path,
                "Exercise,Type,Sets,Reps,Notes\n"
                "Kettlebell swing,Power,3,8,\n"
                "Front squat,Compound,3,8,\n"
                ",,,,\n")
            t = rl.read_template(path)

        self.assertEqual(len(t.exercises), 2)            # blank row dropped
        self.assertEqual(t.primary.name, "Front squat")  # first compound, not the Power row

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            rl.read_template(Path("/no/such/routine.csv"))


class LoadAndRenderTests(unittest.TestCase):

    def test_load_sorted_and_missing_dir_is_empty(self):
        self.assertEqual(rl.load_templates(Path("/no/such/dir")), [])
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d / "Week 2 Routine A.csv", "Exercise,Type,Sets,Reps,Notes\nBack squat,Compound,4,6,\n")
            _write(d / "Week 1 Routine B.csv", "Exercise,Type,Sets,Reps,Notes\nOverhead Press,Compound,3,10,\n")
            templates = rl.load_templates(d)
        self.assertEqual([(t.week, t.slot) for t in templates], [(1, "B"), (2, "A")])

    def test_prompt_block_empty_when_no_templates(self):
        self.assertEqual(rl.to_prompt_block([]), "")

    def test_prompt_block_lists_exercises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Week 1 Routine A.csv"
            _write(path, "Exercise,Type,Sets,Reps,Notes\nBack squat,Compound,4,8,\n")
            block = rl.to_prompt_block([rl.read_template(path)])
        self.assertIn("Week 1 Routine A", block)
        self.assertIn("Back squat (compound) 4x8", block)


class SaveRoundTripTests(unittest.TestCase):

    def test_save_then_read_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Week 1 Routine A.csv"
            _write(path,
                "Exercise,Type,Sets,Reps,Notes\n"
                "Back squat,Compound,4,8,\n"
                "Bulgarian split squat,Compound,3,10,/leg\n")
            original = rl.read_template(path)
            rl.save_template(original)
            reloaded = rl.read_template(path)
        self.assertEqual(original.exercises, reloaded.exercises)


class RealTemplatesParityTests(unittest.TestCase):
    """The six deployed routine CSVs all parse and expose a primary lift."""

    def test_deployed_templates_load(self):
        templates = rl.load_templates()
        self.assertEqual(len(templates), 6)
        names = {t.name for t in templates}
        self.assertIn("Week 1 Routine A", names)
        self.assertIn("Week 2 Routine C", names)
        for t in templates:
            self.assertTrue(t.exercises, f"{t.name} has no exercises")
            self.assertIsNotNone(t.primary)


if __name__ == "__main__":
    unittest.main()
