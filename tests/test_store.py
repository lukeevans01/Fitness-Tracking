"""Tests for store.py — the SQLite storage layer.

Each test runs against a fresh temp database via the FITNESS_DB_PATH env var.
Covers round-trips, overrides lifecycle + cleanup, pending choice, feedback,
weekly nutrition aggregation, and two-profile isolation.

Run from the fitness-emails dir:  python3 -m unittest tests.test_store
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = Path(self._tmp.name) / "app.db"
        self._prev = os.environ.get("FITNESS_DB_PATH")
        os.environ["FITNESS_DB_PATH"] = str(self._db)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FITNESS_DB_PATH", None)
        else:
            os.environ["FITNESS_DB_PATH"] = self._prev
        self._tmp.cleanup()


class TestState(StoreTestCase):
    def test_state_round_trip(self):
        self.assertEqual(store.get_state("luke"), {})
        store.set_state("luke", {"mode": "normal", "cycle_state": "active"})
        self.assertEqual(store.get_state("luke")["mode"], "normal")

    def test_state_upsert_overwrites(self):
        store.set_state("luke", {"mode": "normal"})
        store.set_state("luke", {"mode": "survival"})
        self.assertEqual(store.get_state("luke"), {"mode": "survival"})


class TestOverrides(StoreTestCase):
    def test_set_get_delete(self):
        rec = {"session": {"session_type": "Strength"}}
        store.set_override("luke", "2026-05-27", rec)
        self.assertEqual(store.get_overrides("luke")["2026-05-27"], rec)
        store.delete_override("luke", "2026-05-27")
        self.assertNotIn("2026-05-27", store.get_overrides("luke"))

    def test_override_upsert(self):
        store.set_override("luke", "2026-05-27", {"v": 1})
        store.set_override("luke", "2026-05-27", {"v": 2})
        self.assertEqual(store.get_overrides("luke")["2026-05-27"], {"v": 2})

    def test_clean_old_overrides_cutoff(self):
        store.set_override("luke", "2026-05-20", {"old": True})
        store.set_override("luke", "2026-05-25", {"keep": True})
        removed = store.clean_old_overrides("luke", "2026-05-25")
        self.assertEqual(removed, 1)
        remaining = store.get_overrides("luke")
        self.assertIn("2026-05-25", remaining)
        self.assertNotIn("2026-05-20", remaining)


class TestPendingChoice(StoreTestCase):
    def test_round_trip_and_expiry_field(self):
        self.assertIsNone(store.get_pending_choice("luke"))
        payload = {"expires": "2026-06-03", "chosen": None, "options": {"A": {}}}
        store.set_pending_choice("luke", payload)
        got = store.get_pending_choice("luke")
        self.assertEqual(got["expires"], "2026-06-03")
        self.assertIsNone(got["chosen"])

    def test_mark_chosen(self):
        store.set_pending_choice("luke", {"chosen": None})
        payload = store.get_pending_choice("luke")
        payload["chosen"] = "A"
        store.set_pending_choice("luke", payload)
        self.assertEqual(store.get_pending_choice("luke")["chosen"], "A")


class TestFeedback(StoreTestCase):
    def test_feedback_append_persists(self):
        # No public read API; verify rows land via the underlying table.
        store.append_feedback("luke", {"intent": "food_log"})
        store.append_feedback("luke", {"intent": "revert"})
        conn = store._connect()
        try:
            rows = conn.execute(
                "SELECT entry FROM feedback WHERE profile_id = ? ORDER BY seq", ("luke",)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)


class TestNutrition(StoreTestCase):
    def test_append_and_read_day(self):
        items = [
            {"name": "Eggs", "protein_g": 18, "carbs_g": 1.5, "fat_g": 15, "kcal": 210},
        ]
        self.assertIsNone(store.read_day("luke", "2026-05-27"))
        store.append_nutrition("luke", "2026-05-27", items)
        day = store.read_day("luke", "2026-05-27")
        self.assertEqual(len(day["items"]), 1)
        self.assertIsNotNone(day["first_logged_at"])

    def test_first_logged_at_preserved_across_appends(self):
        store.append_nutrition("luke", "2026-05-27", [{"name": "A", "protein_g": 1}])
        first = store.read_day("luke", "2026-05-27")["first_logged_at"]
        store.append_nutrition("luke", "2026-05-27", [{"name": "B", "protein_g": 2}])
        day = store.read_day("luke", "2026-05-27")
        self.assertEqual(day["first_logged_at"], first)
        self.assertEqual(len(day["items"]), 2)

    def test_weekly_nutrition_aggregation(self):
        store.append_nutrition(
            "luke", "2026-05-27",
            [{"protein_g": 30, "carbs_g": 50, "fat_g": 10, "kcal": 500}],
        )
        store.append_nutrition(
            "luke", "2026-05-26",
            [{"protein_g": 20, "carbs_g": 40, "fat_g": 8, "kcal": 400}],
        )
        out = store.weekly_nutrition("luke", "2026-05-27", days=3)
        self.assertEqual(len(out["days"]), 3)
        by_date = {d["date"]: d for d in out["days"]}
        self.assertTrue(by_date["2026-05-27"]["logged"])
        self.assertEqual(by_date["2026-05-27"]["totals"]["protein_g"], 30)
        self.assertFalse(by_date["2026-05-25"]["logged"])
        self.assertIsNone(by_date["2026-05-25"]["totals"])


class TestAdaptation(StoreTestCase):
    def test_shallow_merge(self):
        store.set_adaptation("luke", {"mode": "normal", "taper_active": False})
        store.set_adaptation("luke", {"taper_active": True})
        got = store.get_adaptation("luke")
        self.assertEqual(got["mode"], "normal")
        self.assertTrue(got["taper_active"])


class TestProfileIsolation(StoreTestCase):
    def test_overrides_are_profile_scoped(self):
        store.set_override("luke", "2026-05-27", {"who": "luke"})
        store.set_override("alex", "2026-05-27", {"who": "alex"})
        self.assertEqual(store.get_overrides("luke")["2026-05-27"], {"who": "luke"})
        self.assertEqual(store.get_overrides("alex")["2026-05-27"], {"who": "alex"})

    def test_state_and_nutrition_isolated(self):
        store.set_state("luke", {"mode": "survival"})
        store.append_nutrition("luke", "2026-05-27", [{"protein_g": 10}])
        # Profile alex sees nothing from luke
        self.assertEqual(store.get_state("alex"), {})
        self.assertIsNone(store.read_day("alex", "2026-05-27"))
        self.assertEqual(store.get_overrides("alex"), {})

    def test_clean_old_overrides_only_affects_target_profile(self):
        store.set_override("luke", "2026-05-20", {})
        store.set_override("alex", "2026-05-20", {})
        store.clean_old_overrides("luke", "2026-05-25")
        self.assertEqual(store.get_overrides("luke"), {})
        self.assertIn("2026-05-20", store.get_overrides("alex"))


if __name__ == "__main__":
    unittest.main()
