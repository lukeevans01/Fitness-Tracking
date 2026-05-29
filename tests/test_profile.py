"""Tests for Pack 06 — the Profile abstraction and its threading."""

import os
import sys
import unittest
from datetime import date
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import coach_orchestrator
import process_replies
from profile import Profile, default_profile, load_profile


class TestLoadProfile(unittest.TestCase):
    def test_luke_profile_fields(self):
        p = load_profile("luke")
        self.assertEqual(p.id, "luke")
        self.assertEqual(p.email, "levans092@gmail.com")
        self.assertEqual(p.race_date, date(2026, 11, 22))
        self.assertEqual(p.race_label, "San Sebastián marathon")
        self.assertEqual(
            p.daily_targets,
            {"protein_g": 130, "carbs_g": 432, "fat_g": 72, "kcal": 2800},
        )
        self.assertIn("Luke Evans", p.profile_text)

    def test_missing_profile_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_profile("does-not-exist")

    def test_default_profile_falls_back_to_luke(self):
        old = os.environ.pop("PROFILE_ID", None)
        try:
            self.assertEqual(default_profile().id, "luke")
        finally:
            if old is not None:
                os.environ["PROFILE_ID"] = old

    def test_default_profile_honours_env(self):
        old = os.environ.get("PROFILE_ID")
        os.environ["PROFILE_ID"] = "luke"
        try:
            self.assertEqual(default_profile().id, "luke")
        finally:
            if old is None:
                os.environ.pop("PROFILE_ID", None)
            else:
                os.environ["PROFILE_ID"] = old


class TestImapSearchQuery(unittest.TestCase):
    def test_search_built_from_profile_email(self):
        p = load_profile("luke")
        self.assertEqual(
            process_replies._imap_search_query(p),
            'UNSEEN FROM "levans092@gmail.com"',
        )

    def test_search_uses_arbitrary_email(self):
        p = Profile(
            id="x", email="other@example.com", display_name="X",
            race_date=date(2027, 1, 1), race_label="R", race_target="t",
            daily_targets={}, profile_text="",
        )
        self.assertEqual(
            process_replies._imap_search_query(p),
            'UNSEEN FROM "other@example.com"',
        )


class TestTaperFromProfile(unittest.TestCase):
    """Taper boundary still holds when race date comes from the profile."""

    def setUp(self):
        self.race = load_profile("luke").race_date  # 2026-11-22

    def test_inactive_29_days_out(self):
        from datetime import timedelta
        d = self.race - timedelta(days=29)
        self.assertFalse(coach_orchestrator.is_taper_active(d, self.race))

    def test_active_28_days_out(self):
        from datetime import timedelta
        d = self.race - timedelta(days=28)
        self.assertTrue(coach_orchestrator.is_taper_active(d, self.race))

    def test_active_on_race_day(self):
        self.assertTrue(coach_orchestrator.is_taper_active(self.race, self.race))

    def test_inactive_after_race(self):
        from datetime import timedelta
        self.assertFalse(
            coach_orchestrator.is_taper_active(self.race + timedelta(days=1), self.race)
        )

    def test_days_to_race_uses_supplied_date(self):
        from datetime import timedelta
        self.assertEqual(
            coach_orchestrator.days_to_race(self.race - timedelta(days=10), self.race), 10
        )


if __name__ == "__main__":
    unittest.main()
