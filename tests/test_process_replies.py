"""Tests for Pack 01 — reply reliability changes in process_replies.py."""

import importlib
import json
import sys
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Minimal stubs so process_replies can be imported without live credentials
# or real Gemini/IMAP connections.
# ---------------------------------------------------------------------------

def _stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _ensure_stubs():
    for name in (
        "coach_orchestrator",
        "intent_classifier",
        "gemini_client",
    ):
        if name not in sys.modules:
            _stub_module(name)

    if "training_summary" not in sys.modules:
        ts = _stub_module("training_summary")
        ts.build_summary = lambda days=14, today=None: ""
        ts.build_stats = lambda days=7, today=None: {"run_sessions": 0, "run_km_total": 0.0, "strength_sessions": 0}

    if "weekly_load" not in sys.modules:
        wl = _stub_module("weekly_load")
        wl.build_weekly_load = lambda days=7, today=None, profile_id=None: None

    if "nutrition_logger" not in sys.modules:
        from dataclasses import dataclass

        nl = _stub_module("nutrition_logger")

        @dataclass
        class _LogResult:
            items: list
            running_totals: dict
            delta_vs_target: dict
            coach_note: str = ""

        nl.LogResult = _LogResult
        nl.DAILY_TARGETS = {"protein_g": 130, "carbs_g": 432, "fat_g": 72, "kcal": 2800}
        nl.read_day = lambda d, profile_id=None: None
        nl.log_food = lambda text, d, profile=None: _LogResult([], {}, {})
        nl.daily_totals = lambda log: {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}
        nl.weekly_summary = lambda days=7, end_date=None, targets=None, profile_id=None: {
            "days_logged": 0, "avg_protein_g": 0.0, "avg_carbs_g": 0.0,
            "avg_fat_g": 0.0, "avg_kcal": 0.0, "protein_target_hits": 0,
            "lowest_protein_day": None, "patterns": [],
        }

    # send_daily exports used by process_replies
    if "send_daily" not in sys.modules:
        sd = _stub_module("send_daily")
        sd.CSS_BASE = ""
        sd.build_cycle_html = (
            lambda *a, **kw: '<!DOCTYPE html><html><body style="">content</body></html>'
        )
        sd.build_cycle_text = lambda *a, **kw: ""

    # specialists sub-package
    if "specialists" not in sys.modules:
        _stub_module("specialists")


_ensure_stubs()

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import process_replies  # noqa: E402
from profile import load_profile  # noqa: E402

_TEST_PROFILE = load_profile("luke")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_imap(msg_ids: list, raw_bytes: bytes = b"") -> MagicMock:
    """Return a mock IMAP4_SSL-like object."""
    mail = MagicMock()
    mail.search.return_value = (None, [b" ".join(msg_ids)])
    mail.fetch.return_value = (None, [(None, raw_bytes)])
    return mail


def _minimal_email_bytes(body: str = "hello") -> bytes:
    return (
        f"From: Luke Evans <levans092@gmail.com>\r\n"
        f"Message-ID: <test-id@gmail.com>\r\n"
        f"Subject: test\r\n"
        f"Content-Type: text/plain\r\n\r\n"
        f"{body}"
    ).encode()


# ---------------------------------------------------------------------------
# Test 1: message marked \Seen exactly once even when a handler raises
# ---------------------------------------------------------------------------

class TestSeenOnException(unittest.TestCase):
    def test_seen_called_once_on_handler_exception(self):
        """Finally block marks message Seen even when dispatch raises."""
        mail = _make_imap([b"1"], _minimal_email_bytes("change tomorrow to rest"))

        with (
            patch.object(process_replies, "GMAIL_USER", "bot@gmail.com"),
            patch.object(process_replies, "GMAIL_PASSWORD", "password"),
            patch.object(process_replies, "RESEND_API_KEY", "re_test"),
            patch.object(process_replies, "imaplib") as mock_imap_mod,
            patch.object(process_replies, "_detect_mode_command", return_value=None),
            patch.object(process_replies, "_RE_WEEK_CHOICE") as mock_re,
            patch.object(process_replies, "_RE_REVERT") as mock_revert_re,
            patch.object(process_replies, "intent_classifier") as mock_clf,
            patch.object(process_replies, "_handle_training_feedback",
                         side_effect=RuntimeError("boom")),
            patch.object(process_replies, "_send_plain_notice"),
            patch.object(process_replies, "_append_feedback_log"),
            patch.object(process_replies, "coach_orchestrator") as mock_orch,
            patch.object(process_replies.store, "get_state",
                         return_value={"mode": "normal", "week_choice": ""}),
            patch.object(process_replies, "_load_json", return_value={}),
            patch.object(process_replies, "_load_overrides",
                         return_value={"overrides": {}}),
            patch.object(process_replies, "_clean_old_overrides"),
        ):
            mock_imap_mod.IMAP4_SSL.return_value = mail
            mock_re.match.return_value = None
            mock_revert_re.search.return_value = None
            mock_orch.sync_taper_state.return_value = None
            mock_clf.classify.return_value = {
                "intents": [{"intent": "training_feedback", "text": "change tomorrow to rest"}]
            }

            process_replies.main()

        store_calls = [c for c in mail.store.call_args_list if "\\Seen" in str(c)]
        self.assertEqual(len(store_calls), 1, f"Expected 1 Seen call, got {store_calls}")


# ---------------------------------------------------------------------------
# Test 2: _handle_training_feedback persists the override (to the store) before _send_email
# ---------------------------------------------------------------------------

class TestOverridePersistedBeforeSend(unittest.TestCase):
    def test_override_persisted_before_send_email(self):
        """Override is written to the store before the replacement email is sent."""
        valid_session = {
            "session_type": "Easy run",
            "session_kind": "run",
            "duration_min": 40,
            "short_version": "20 min jog",
            "purpose": "aerobic base",
            "coach_note": "Swapped to run.",
            "warm_up": "5 min walk",
            "exercises": [],
            "run_details": {"pace": "6:00/km", "hr_target": "<150", "duration": "40 min",
                            "distance": "6 km", "effort": "easy"},
            "extras": "",
        }

        call_order = []
        overrides = {"overrides": {}}

        def fake_set_override(profile_id, iso_date, record):
            call_order.append("save")

        def fake_send_email(subject, html, text):
            call_order.append("send")
            # The override must be in the in-memory cache by send time.
            assert overrides["overrides"], "Override not persisted at send time"
            return True

        with (
            patch.object(process_replies, "coach_orchestrator") as mock_orch,
            patch.object(process_replies, "_send_email", side_effect=fake_send_email),
            patch.object(process_replies.store, "set_override", side_effect=fake_set_override),
            patch.object(process_replies, "_append_feedback_log"),
            patch.object(process_replies, "ts") as mock_ts,
        ):
            mock_orch.infer_domain.return_value = "run"
            mock_orch.generate_session.return_value = valid_session
            mock_ts.build_summary.return_value = ""

            plan = {
                "cycle_start_date": "2026-05-25",
                "cycle_length_days": 10,
                "cycle_days": [
                    {
                        "day_num": i,
                        "session_type": "Easy run",
                        "session_kind": "run",
                        "duration_min": 40,
                        "short_version": "20 min jog",
                        "purpose": "base",
                        "warm_up": "",
                        "exercises": [],
                        "run_details": {"pace": "", "hr_target": "", "duration": "",
                                        "distance": "", "effort": ""},
                        "extras": "",
                    }
                    for i in range(1, 11)
                ],
                "hard_rules": [],
            }
            tomorrow = date(2026, 5, 30)

            process_replies._handle_training_feedback(
                text="swap to easy run",
                plan=plan,
                overrides=overrides,
                target_date=tomorrow,
                message_id="<test@test>",
                from_addr="levans092@gmail.com",
                week_context="",
                profile=_TEST_PROFILE,
            )

        self.assertEqual(call_order, ["save", "send"],
                         f"Expected save before send, got: {call_order}")


class TestFutureTargetDate(unittest.TestCase):
    """Pack 11: a reply naming a future day writes the override under THAT date."""

    def test_future_override_written_and_emailed_for_that_date(self):
        valid_session = {
            "session_type": "Lift", "session_kind": "strength", "duration_min": 60,
            "short_version": "top sets only", "purpose": "strength", "coach_note": "Made it a lift.",
            "warm_up": "", "exercises": [], "run_details": {}, "extras": "",
        }
        target = date(2026, 6, 11)  # well beyond tomorrow
        saved = {}
        emailed_dates = []

        def fake_set_override(profile_id, iso_date, record):
            saved["iso"] = iso_date

        def fake_build_email(session, coach_note, target_date, plan):
            emailed_dates.append(target_date)
            return ("subj", "<html>", "text")

        overrides = {"overrides": {}}
        plan = {
            "cycle_start_date": "2026-05-25",
            "cycle_length_days": 7,
            "cycle_days": [
                {"day_num": i, "session_type": "Easy run", "session_kind": "run",
                 "duration_min": 40, "short_version": "x", "purpose": "base", "warm_up": "",
                 "exercises": [], "run_details": {"pace": "", "hr_target": "", "duration": "",
                                                  "distance": "", "effort": ""}, "extras": ""}
                for i in range(1, 8)
            ],
            "hard_rules": [],
        }

        with (
            patch.object(process_replies, "coach_orchestrator") as mock_orch,
            patch.object(process_replies, "_send_email", return_value=True),
            patch.object(process_replies, "_build_replacement_email", side_effect=fake_build_email),
            patch.object(process_replies.store, "set_override", side_effect=fake_set_override),
            patch.object(process_replies, "_append_feedback_log"),
            patch.object(process_replies, "ts") as mock_ts,
            patch.object(process_replies, "weekly_load") as mock_wl,
        ):
            mock_orch.infer_domain.return_value = "strength"
            mock_orch.generate_session.return_value = valid_session
            mock_ts.build_summary.return_value = ""
            mock_wl.build_weekly_load.return_value = None

            process_replies._handle_training_feedback(
                text="make it a lift",
                plan=plan,
                overrides=overrides,
                target_date=target,
                message_id="<test@test>",
                from_addr="levans092@gmail.com",
                week_context="",
                profile=_TEST_PROFILE,
            )

        self.assertEqual(saved["iso"], target.isoformat())
        self.assertIn(target.isoformat(), overrides["overrides"])
        self.assertEqual(emailed_dates, [target])


# ---------------------------------------------------------------------------
# Test 3: _clean_old_overrides date logic
# ---------------------------------------------------------------------------

class TestCleanOldOverrides(unittest.TestCase):
    def test_removes_past_keeps_future(self):
        # Pack 11: prune only past dates; a deliberate future-dated edit survives until
        # its date passes, even if it is weeks out.
        today = date(2026, 5, 28)
        past_key = (today - timedelta(days=2)).isoformat()    # should be removed
        future_key = (today + timedelta(days=21)).isoformat()  # should be kept

        overrides = {"overrides": {past_key: {"session": {}}, future_key: {"session": {}}}}
        with patch.object(process_replies.store, "clean_old_overrides", return_value=1):
            process_replies._clean_old_overrides(overrides, today)

        self.assertNotIn(past_key, overrides["overrides"])
        self.assertIn(future_key, overrides["overrides"])

    def test_keeps_today(self):
        today = date(2026, 5, 28)
        overrides = {"overrides": {today.isoformat(): {"session": {}}}}
        with patch.object(process_replies.store, "clean_old_overrides", return_value=0):
            process_replies._clean_old_overrides(overrides, today)
        self.assertIn(today.isoformat(), overrides["overrides"])


# ---------------------------------------------------------------------------
# Test 4: _is_nutrition_actionable gating
# ---------------------------------------------------------------------------

class TestIsNutritionActionable(unittest.TestCase):
    def _make_result(self, coach_note="", protein_delta=0, kcal_delta=0):
        """Build a minimal LogResult-like object."""
        from nutrition_logger import LogResult
        return LogResult(
            items=[],
            running_totals={"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0},
            delta_vs_target={"protein_g": protein_delta, "carbs_g": 0.0, "fat_g": 0.0, "kcal": kcal_delta},
            coach_note=coach_note,
        )

    def test_coach_note_triggers_email(self):
        result = self._make_result(coach_note="Hit your protein target tonight.")
        self.assertTrue(process_replies._is_nutrition_actionable(result))

    def test_protein_short_by_31g_triggers_email(self):
        result = self._make_result(protein_delta=-31)
        self.assertTrue(process_replies._is_nutrition_actionable(result))

    def test_protein_short_by_30g_does_not_trigger(self):
        result = self._make_result(protein_delta=-30)
        self.assertFalse(process_replies._is_nutrition_actionable(result))

    def test_kcal_over_by_501_triggers_email(self):
        result = self._make_result(kcal_delta=501)
        self.assertTrue(process_replies._is_nutrition_actionable(result))

    def test_kcal_under_by_501_triggers_email(self):
        result = self._make_result(kcal_delta=-501)
        self.assertTrue(process_replies._is_nutrition_actionable(result))

    def test_kcal_exactly_500_does_not_trigger(self):
        result = self._make_result(kcal_delta=500)
        self.assertFalse(process_replies._is_nutrition_actionable(result))

    def test_on_target_is_silent(self):
        result = self._make_result()
        self.assertFalse(process_replies._is_nutrition_actionable(result))


# ---------------------------------------------------------------------------
# Test 5: _handle_food_log sends email only when actionable
# ---------------------------------------------------------------------------

class TestFoodLogEmailGating(unittest.TestCase):
    def _run_handle(self, result):
        send_calls = []
        log_entries = []

        with (
            patch.object(process_replies, "nutrition_logger") as mock_nl,
            patch.object(process_replies, "_send_email", side_effect=lambda s, h, t: send_calls.append(s) or True),
            patch.object(process_replies, "_append_feedback_log", side_effect=log_entries.append),
        ):
            mock_nl.log_food.return_value = result
            mock_nl.DAILY_TARGETS = {"protein_g": 130, "carbs_g": 432, "fat_g": 72, "kcal": 2800}
            process_replies._handle_food_log(
                "chicken and rice", "<msg@test>", "levans092@gmail.com", _TEST_PROFILE,
            )

        return send_calls, log_entries

    def test_actionable_result_sends_email(self):
        from nutrition_logger import LogResult
        result = LogResult(
            items=[],
            running_totals={"protein_g": 50.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 500.0},
            delta_vs_target={"protein_g": -80, "carbs_g": 0.0, "fat_g": 0.0, "kcal": -2300},
            coach_note="",
        )
        send_calls, log_entries = self._run_handle(result)
        self.assertEqual(len(send_calls), 1)
        self.assertTrue(log_entries[0]["emailed"])

    def test_on_target_result_does_not_send_email(self):
        from nutrition_logger import LogResult
        result = LogResult(
            items=[],
            running_totals={"protein_g": 130.0, "carbs_g": 432.0, "fat_g": 72.0, "kcal": 2800.0},
            delta_vs_target={"protein_g": 0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0},
            coach_note="",
        )
        send_calls, log_entries = self._run_handle(result)
        self.assertEqual(len(send_calls), 0)
        self.assertFalse(log_entries[0]["emailed"])


# ---------------------------------------------------------------------------
# Test 6: _handle_mobility_log does not send any email
# ---------------------------------------------------------------------------

class TestMobilityLogSilent(unittest.TestCase):
    def test_no_email_sent_for_mobility_log(self):
        send_calls = []
        with (
            patch.object(process_replies, "_send_plain_notice",
                         side_effect=lambda s, b: send_calls.append(s)),
            patch.object(process_replies, "_send_email",
                         side_effect=lambda s, h, t: send_calls.append(s)),
            patch.object(process_replies, "_append_feedback_log"),
        ):
            process_replies._handle_mobility_log(
                "20 min yoga, hips tight", "<msg@test>", "levans092@gmail.com"
            )
        self.assertEqual(send_calls, [], "No email should be sent for a mobility log")


if __name__ == "__main__":
    unittest.main()
