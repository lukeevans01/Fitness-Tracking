"""Unit tests for nutrition_logger.

Mocks coach_orchestrator.generate_food_log_response and nutrition_lookup.lookup_food
so no Gemini or OFF network calls are made. Storage goes through store.py against a
temp SQLite database (FITNESS_DB_PATH) created per test.

Run from the fitness-emails dir:  python3 -m unittest tests.test_nutrition_logger
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nutrition_logger  # noqa: E402
from nutrition_logger import DayLog, FoodItem  # noqa: E402


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
        self._db = Path(self._tmp.name) / "app.db"
        self._prev_db = os.environ.get("FITNESS_DB_PATH")
        os.environ["FITNESS_DB_PATH"] = str(self._db)

    def tearDown(self):
        if self._prev_db is None:
            os.environ.pop("FITNESS_DB_PATH", None)
        else:
            os.environ["FITNESS_DB_PATH"] = self._prev_db
        self._tmp.cleanup()

    # ── log_food persists items readable through the store ─────────────

    def test_log_food_persists_items(self):
        items = [_gemini_item(source="gemini")]  # skip OFF lookup
        target = date(2026, 5, 27)
        with _gemini_returns(items):
            result = nutrition_logger.log_food("3 eggs", target)

        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].name, "Eggs")
        day = nutrition_logger.read_day(target)
        self.assertIsNotNone(day)
        self.assertEqual(len(day.items), 1)
        self.assertEqual(day.items[0].meal, "breakfast")
        self.assertEqual(day.items[0].quantity, "3 large")

    # ── append: second call adds rows, doesn't overwrite ──────────────

    def test_log_food_appends_on_second_call(self):
        target = date(2026, 5, 27)
        with _gemini_returns([_gemini_item(name="Eggs", source="gemini")]):
            nutrition_logger.log_food("3 eggs", target)
        with _gemini_returns([_gemini_item(name="Toast", source="gemini", meal="breakfast")]):
            result = nutrition_logger.log_food("toast", target)

        day = nutrition_logger.read_day(target)
        names = [i.name for i in day.items]
        self.assertEqual(names, ["Eggs", "Toast"])
        # running_totals after second call reflect both items
        self.assertEqual(result.running_totals["protein_g"], 36.0)

    # ── read_day round-trip ───────────────────────────────────────────

    def test_read_day_round_trips(self):
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

    # ── read_day on a day with nothing logged returns None ────────────

    def test_read_day_missing_returns_none(self):
        self.assertIsNone(nutrition_logger.read_day(date(2026, 1, 1)))

    # ── full precision is preserved in the store (no truncation) ──────

    def test_stored_macros_keep_full_precision(self):
        item = _gemini_item(
            name="Precise meal", source="gemini",
            kcal=123.456, protein_g=12.34, carbs_g=5.678, fat_g=9.012,
        )
        target = date(2026, 5, 27)
        with _gemini_returns([item]):
            nutrition_logger.log_food("precise meal", target)

        day = nutrition_logger.read_day(target)
        stored = day.items[0]
        # Values come back at full float precision — not rounded to the 1-dp
        # display format used in the markdown/email view.
        self.assertEqual(stored.kcal, 123.456)
        self.assertEqual(stored.protein_g, 12.34)
        self.assertEqual(stored.carbs_g, 5.678)
        self.assertEqual(stored.fat_g, 9.012)

    # ── render_day_markdown is a display-only view ────────────────────

    def test_render_day_markdown_is_display_only(self):
        items = [
            _gemini_item(name="Eggs", source="gemini", protein_g=18.36, carbs_g=1.5,
                         fat_g=15.0, kcal=210),
        ]
        target = date(2026, 5, 27)
        with _gemini_returns(items):
            nutrition_logger.log_food("eggs", target)

        day = nutrition_logger.read_day(target)
        md = nutrition_logger.render_day_markdown(day)
        self.assertIn("schema_version: 1", md)
        self.assertIn("log_date: 2026-05-27", md)
        self.assertIn("| breakfast | Eggs | 3 large |", md)
        self.assertIn("## Daily totals", md)
        # Stored value keeps full precision even though the view rounds to 1 dp.
        self.assertEqual(day.items[0].protein_g, 18.36)
        self.assertIn("18.4", md)  # presentation rounding only

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
        # Log on two days of a 5-day window
        end = date(2026, 5, 27)
        for offset in (4, 2):
            d = end - timedelta(days=offset)
            with _gemini_returns([_gemini_item(name="Eggs", source="gemini")]):
                nutrition_logger.log_food("3 eggs", d)
        weekly = nutrition_logger.weekly_summary(days=5, end_date=end)
        self.assertEqual(weekly["days_logged"], 2)
        self.assertEqual(weekly["avg_protein_g"], 18.0)
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
        item = _gemini_item(
            name="Banana",
            quantity="1 medium",
            quantity_g=120,
            kcal=999,
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
        with _gemini_returns([item]), _off_returns(""):
            result = nutrition_logger.log_food("mystery meal", date(2026, 5, 27))

        logged = result.items[0]
        self.assertEqual(logged.source, "gemini")
        self.assertEqual(logged.kcal, 500)
        self.assertEqual(logged.protein_g, 30)


if __name__ == "__main__":
    unittest.main()
