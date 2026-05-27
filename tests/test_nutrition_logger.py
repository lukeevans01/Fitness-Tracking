"""Unit tests for nutrition_logger.

Mocks coach_orchestrator.generate_food_log_response and nutrition_lookup.lookup_food
so no Gemini or OFF network calls are made. Patches LOG_DIR to a tmp path per test.

Run from the fitness-emails dir:  python -m unittest tests.test_nutrition_logger
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nutrition_logger  # noqa: E402
from nutrition_logger import DAILY_TARGETS, DayLog, FoodItem  # noqa: E402


def _gemini_returns(items, coach_note=""):
    """Patch coach_orchestrator.generate_food_log_response to return a canned result."""
    return patch.object(
        nutrition_logger.coach_orchestrator,
        "generate_food_log_response",
        return_value={"items": items, "coach_note": coach_note},
    )


def _off_returns(value):
    """Patch nutrition_lookup.lookup_food to return a canned string ('' = OFF miss)."""
    return patch.object(
        nutrition_logger.nutrition_lookup,
        "lookup_food",
        return_value=value,
    )


def _gemini_item(**overrides):
    """A complete Gemini item dict with sensible defaults for tests."""
    base = {
        "name": "Eggs",
        "quantity": "3 large",
        "quantity_g": 150,
        "kcal": 210,
        "protein_g": 18.0,
        "carbs_g": 1.5,
        "fat_g": 15.0,
        "confidence": "high",
        "source": "needs_lookup",
        "meal": "breakfast",
    }
    base.update(overrides)
    return base


class NutritionLoggerTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name) / "nutrition_log"
        self._log_dir_patch = patch.object(nutrition_logger, "LOG_DIR", self._dir)
        self._log_dir_patch.start()

    def tearDown(self):
        self._log_dir_patch.stop()
        self._tmp.cleanup()

    # ── log_food writes a well-formed file ──────────────────────────────

    def test_log_food_writes_well_formed_file(self):
        items = [_gemini_item(source="gemini")]  # skip OFF lookup
        target = date(2026, 5, 27)
        with _gemini_returns(items):
            result = nutrition_logger.log_food("3 eggs", target)

        path = self._dir / "2026-05-27.md"
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("schema_version: 1", content)
        self.assertIn("log_date: 2026-05-27", content)
        self.assertIn("first_logged_at:", content)
        self.assertIn("last_updated_at:", content)
        self.assertIn("| breakfast | Eggs | 3 large |", content)
        self.assertIn("## Daily totals", content)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].name, "Eggs")

    # ── append: second call adds rows, doesn't overwrite ──────────────

    def test_log_food_appends_on_second_call(self):
        target = date(2026, 5, 27)
        with _gemini_returns([_gemini_item(name="Eggs", source="gemini")]):
            nutrition_logger.log_food("3 eggs", target)
        with _gemini_returns([_gemini_item(name="Toast", source="gemini", meal="breakfast")]):
            result = nutrition_logger.log_food("toast", target)

        content = (self._dir / "2026-05-27.md").read_text()
        self.assertIn("| breakfast | Eggs |", content)
        self.assertIn("| breakfast | Toast |", content)
        # first_logged_at preserved, last_updated_at advances
        self.assertEqual(content.count("schema_version: 1"), 1)
        # running_totals after second call reflect both items
        self.assertEqual(result.running_totals["protein_g"], 36.0)

    # ── read_day round-trip ───────────────────────────────────────────

    def test_read_day_round_trips_a_written_file(self):
        items = [
            _gemini_item(name="Eggs", source="gemini", protein_g=18.0, carbs_g=1.5,
                         fat_g=15.0, kcal=210),
            _gemini_item(name="Toast", source="gemini", quantity="2 slices",
                         protein_g=6.0, carbs_g=30.0, fat_g=2.0, kcal=160,
                         meal="breakfast"),
        ]
        target = date(2026, 5, 27)
        with _gemini_returns(items):
            nutrition_logger.log_food("eggs and toast", target)

        day = nutrition_logger.read_day(target)
        self.assertIsNotNone(day)
        self.assertEqual(day.log_date, target)
        self.assertEqual(day.schema_version, 1)
        self.assertEqual(len(day.items), 2)
        self.assertEqual(day.items[0].name, "Eggs")
        self.assertAlmostEqual(day.items[1].carbs_g, 30.0)

    # ── read_day on missing file returns None, not raises ─────────────

    def test_read_day_missing_file_returns_none(self):
        self.assertIsNone(nutrition_logger.read_day(date(2026, 1, 1)))

    # ── read_day tolerates a malformed row ────────────────────────────

    def test_read_day_skips_malformed_row_keeps_valid(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / "2026-05-27.md"
        path.write_text(
            "---\n"
            "schema_version: 1\n"
            "log_date: 2026-05-27\n"
            "first_logged_at: 2026-05-27T08:00:00+02:00\n"
            "last_updated_at: 2026-05-27T08:00:00+02:00\n"
            "---\n\n"
            "# Nutrition log\n\n"
            "## Items\n\n"
            "| Meal | Item | Quantity | kcal | P (g) | C (g) | F (g) | Confidence | Source |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| breakfast | Eggs | 3 large | 210 | 18.0 | 1.5 | 15.0 | high | off |\n"
            "| breakfast | Broken Row | three | not-a-number | nope | nope | nope | low | gemini |\n"
            "| lunch | Banana | 1 medium | 105 | 1.3 | 27.0 | 0.4 | high | off |\n\n"
            "## Daily totals\n\n"
            "- **Calories:** 315 / 2800 (-2485)\n"
        )
        day = nutrition_logger.read_day(date(2026, 5, 27))
        self.assertIsNotNone(day)
        names = [i.name for i in day.items]
        self.assertEqual(names, ["Eggs", "Banana"])

    # ── daily_totals sums correctly ───────────────────────────────────

    def test_daily_totals_sums_items(self):
        items = [
            FoodItem("Eggs", "3 large", 210, 18.0, 1.5, 15.0, "high", "off", "breakfast"),
            FoodItem("Toast", "2 slices", 160, 6.0, 30.0, 2.0, "high", "off", "breakfast"),
        ]
        log = DayLog(log_date=date(2026, 5, 27), schema_version=1, items=items)
        totals = nutrition_logger.daily_totals(log)
        self.assertEqual(totals["kcal"], 370)
        self.assertEqual(totals["protein_g"], 24.0)
        self.assertEqual(totals["carbs_g"], 31.5)

    def test_daily_totals_none_returns_zeros(self):
        totals = nutrition_logger.daily_totals(None)
        self.assertEqual(totals["protein_g"], 0.0)
        self.assertEqual(totals["kcal"], 0.0)

    # ── weekly_summary handles missing days ───────────────────────────

    def test_weekly_summary_counts_only_logged_days(self):
        # Log on days 1 and 3 of a 5-day window
        end = date(2026, 5, 27)
        for offset in (4, 2):  # days_ago = 4 and 2 in a 5-day window
            d = end - timedelta(days=offset)
            with _gemini_returns([_gemini_item(name="Eggs", source="gemini")]):
                nutrition_logger.log_food("3 eggs", d)
        weekly = nutrition_logger.weekly_summary(days=5, end_date=end)
        self.assertEqual(weekly["days_logged"], 2)
        self.assertEqual(weekly["avg_protein_g"], 18.0)
        # Three days have no log -> pattern should fire about gaps
        gap_patterns = [p for p in weekly["patterns"] if p.startswith("no log for")]
        self.assertEqual(len(gap_patterns), 1)

    # ── pattern: protein <80% target for 3 consecutive days ──────────

    def test_pattern_fires_for_3_consecutive_low_protein_days(self):
        end = date(2026, 5, 27)
        low_protein_item = _gemini_item(
            name="Low protein meal", source="gemini",
            protein_g=20.0, carbs_g=80.0, fat_g=10.0, kcal=500,
        )
        for offset in range(3):
            d = end - timedelta(days=offset)
            with _gemini_returns([low_protein_item]):
                nutrition_logger.log_food("light meal", d)
        weekly = nutrition_logger.weekly_summary(days=3, end_date=end)
        self.assertEqual(weekly["days_logged"], 3)
        low_p = [p for p in weekly["patterns"] if "consecutive days" in p]
        self.assertEqual(len(low_p), 1)
        self.assertIn("3 consecutive days", low_p[0])

    # ── pattern: avg kcal <2200 fires under-eating flag ──────────────

    def test_pattern_fires_for_low_avg_kcal(self):
        end = date(2026, 5, 27)
        skimpy = _gemini_item(
            name="Skimpy", source="gemini",
            protein_g=130.0, carbs_g=100.0, fat_g=20.0, kcal=1000,
        )
        for offset in range(3):
            d = end - timedelta(days=offset)
            with _gemini_returns([skimpy]):
                nutrition_logger.log_food("snack", d)
        weekly = nutrition_logger.weekly_summary(days=3, end_date=end)
        kcal_patterns = [p for p in weekly["patterns"] if "below 2,200" in p]
        self.assertEqual(len(kcal_patterns), 1)

    # ── OFF override: needs_lookup with OFF hit scales by quantity_g ──

    def test_off_lookup_overrides_gemini_macros(self):
        # OFF data for "Banana": 89 kcal, 23g carbs, 1.1g protein, 0.3g fat per 100g
        # Gemini says it's 120g -> expect 89*1.2=106.8 kcal, etc.
        item = _gemini_item(
            name="Banana",
            quantity="1 medium",
            quantity_g=120,
            kcal=999,  # Gemini's fallback — should be overridden
            protein_g=999,
            carbs_g=999,
            fat_g=999,
            source="needs_lookup",
        )
        off_str = "Banana: 89 kcal, 23.0g carbs, 1.1g protein, 0.3g fat (per 100g)"
        with _gemini_returns([item]), _off_returns(off_str):
            result = nutrition_logger.log_food("a banana", date(2026, 5, 27))

        logged = result.items[0]
        self.assertEqual(logged.source, "off")
        self.assertAlmostEqual(logged.kcal, 89 * 1.2)
        self.assertAlmostEqual(logged.carbs_g, 23.0 * 1.2)
        self.assertAlmostEqual(logged.protein_g, 1.1 * 1.2)
        self.assertAlmostEqual(logged.fat_g, 0.3 * 1.2, places=2)

    # ── OFF miss: needs_lookup falls back to Gemini macros ───────────

    def test_off_miss_falls_back_to_gemini(self):
        item = _gemini_item(
            name="Unknown Composite Meal",
            source="needs_lookup",
            kcal=500, protein_g=30, carbs_g=60, fat_g=15,
        )
        with _gemini_returns([item]), _off_returns(""):  # OFF returns nothing
            result = nutrition_logger.log_food("mystery meal", date(2026, 5, 27))

        logged = result.items[0]
        self.assertEqual(logged.source, "gemini")
        self.assertEqual(logged.kcal, 500)
        self.assertEqual(logged.protein_g, 30)


if __name__ == "__main__":
    unittest.main()
