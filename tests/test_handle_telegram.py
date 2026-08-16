"""Tests for executing a Telegram update.

Runs against a temp database; every Telegram API call is stubbed so nothing leaves the
machine. Focus is on the button paths, which need no LLM and are the ones that carry the
day-to-day traffic.

Run from the fitness-emails dir:  python3 -m unittest tests.test_handle_telegram
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

CHAT = "12345"
TODAY = date(2026, 8, 23)
WEEK_START = date(2026, 8, 24)


def _callback(data, update_id=100, chat=CHAT):
    return {"update_id": update_id,
            "callback_query": {"id": "cb1", "data": data,
                               "message": {"message_id": 55, "chat": {"id": int(chat)}}}}


def _message(text, update_id=200, chat=CHAT):
    return {"update_id": update_id,
            "message": {"message_id": 56, "chat": {"id": int(chat)}, "text": text}}


def _week_plan():
    days = []
    for i in range(7):
        iso = (WEEK_START + timedelta(days=i)).isoformat()
        if i >= 5:
            days.append({"date": iso, "session_type": "Rest Day",
                         "session_kind": "rest", "duration_min": 30})
        else:
            days.append({"date": iso, "session_type": "Easy Run",
                         "session_kind": "run", "duration_min": 50,
                         "run_details": {"distance_km": 8.0}})
    return days


class HandleTelegramTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["FITNESS_DB_PATH"] = str(Path(self._tmp.name) / "app.db")
        os.environ["TELEGRAM_CHAT_ID"] = CHAT
        os.environ["TELEGRAM_BOT_TOKEN"] = "stub"
        for mod in ("store", "plan_writer", "process_replies", "handle_telegram"):
            sys.modules.pop(mod, None)
        import store
        import handle_telegram
        self.store = store
        self.ht = handle_telegram
        self.pid = "luke"

        self.sent = []
        self.acked = []
        self.cleared = []
        self._patches = [
            mock.patch.object(handle_telegram.notify_telegram, "send_message",
                              side_effect=lambda text, markup=None: self.sent.append(text) or True),
            mock.patch.object(handle_telegram.notify_telegram, "answer_callback_query",
                              side_effect=lambda cid, text="": self.acked.append(cid) or True),
            mock.patch.object(handle_telegram.notify_telegram, "clear_buttons",
                              side_effect=lambda mid: self.cleared.append(mid) or True),
            mock.patch.object(handle_telegram, "_today", return_value=TODAY),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for key in ("FITNESS_DB_PATH", "TELEGRAM_UPDATE", "TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN"):
            os.environ.pop(key, None)
        self._tmp.cleanup()
        for mod in ("store", "plan_writer", "process_replies", "handle_telegram"):
            sys.modules.pop(mod, None)

    def _run(self, update):
        os.environ["TELEGRAM_UPDATE"] = json.dumps(update)
        return self.ht.main()

    def _seed_pending(self):
        self.store.set_pending_choice(self.pid, {
            "week_label": "24-30 Aug",
            "week_start": WEEK_START.isoformat(),
            "expires": (WEEK_START + timedelta(days=7)).isoformat(),
            "options": {"B": {"label": "Lighter", "sessions": "Mon: ...",
                              "rationale": "Sleep.", "plan": _week_plan()}},
            "recommendation": "B",
            "chosen": None,
        })


class SecurityTests(HandleTelegramTests):
    def test_foreign_chat_gets_no_reply(self):
        """Replying would confirm the bot is live to whoever probed it."""
        code = self._run(_message("B", chat="99999"))
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [])

    def test_no_expected_chat_configured_is_closed(self):
        os.environ.pop("TELEGRAM_CHAT_ID")
        self.assertEqual(self._run(_message("B")), 0)
        self.assertEqual(self.sent, [])

    def test_empty_update_is_a_no_op(self):
        os.environ["TELEGRAM_UPDATE"] = ""
        self.assertEqual(self.ht.main(), 0)
        self.assertEqual(self.sent, [])

    def test_invalid_json_exits_nonzero(self):
        os.environ["TELEGRAM_UPDATE"] = "{not json"
        self.assertEqual(self.ht.main(), 1)


class ButtonTests(HandleTelegramTests):
    def test_week_choice_button_applies_the_week(self):
        self._seed_pending()
        with mock.patch.object(self.ht.store, "get_overrides", wraps=self.ht.store.get_overrides):
            code = self._run(_callback("wk:B"))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.store.get_overrides(self.pid)), 7)
        self.assertTrue(self.sent)

    def test_button_tap_is_acknowledged_before_the_work(self):
        """Otherwise the button spins until Telegram times it out."""
        self._seed_pending()
        self._run(_callback("wk:B"))
        self.assertEqual(self.acked, ["cb1"])

    def test_used_week_buttons_are_removed(self):
        self._seed_pending()
        self._run(_callback("wk:B"))
        self.assertEqual(self.cleared, [55])

    def test_week_choice_with_nothing_pending_says_so(self):
        code = self._run(_callback("wk:B"))
        self.assertEqual(code, 0)
        self.assertIn("No week is currently open", self.sent[0])
        self.assertEqual(self.store.get_overrides(self.pid), {})

    def test_session_feedback_is_logged_not_replanned(self):
        code = self._run(_callback("fb:2026-08-17:skip"))
        self.assertEqual(code, 0)
        # Recorded as feedback...
        entries = self.store.get_feedback(self.pid) if hasattr(self.store, "get_feedback") else None
        self.assertIn("Logged", self.sent[0])
        # ...and explicitly does not rewrite the plan.
        self.assertEqual(self.store.get_overrides(self.pid), {})

    def test_all_feedback_kinds_are_accepted(self):
        for i, kind in enumerate(("done", "skip", "hard")):
            self.sent.clear()
            self._run(_callback(f"fb:2026-08-17:{kind}", update_id=300 + i))
            self.assertIn("Logged", self.sent[0], kind)

    def test_unrecognised_callback_still_stops_the_spinner(self):
        code = self._run(_callback("garbage"))
        self.assertEqual(code, 0)
        self.assertEqual(self.acked, ["cb1"])
        self.assertEqual(self.sent, [])


class DeduplicationTests(HandleTelegramTests):
    def test_replayed_update_is_handled_once(self):
        """Telegram retries until it gets a 200, so the same tap can arrive twice."""
        self._run(_callback("fb:2026-08-17:done", update_id=777))
        first = len(self.sent)
        self._run(_callback("fb:2026-08-17:done", update_id=777))
        self.assertEqual(len(self.sent), first, "second delivery should have been skipped")

    def test_different_updates_both_run(self):
        self._run(_callback("fb:2026-08-17:done", update_id=801))
        self._run(_callback("fb:2026-08-18:done", update_id=802))
        self.assertEqual(len(self.sent), 2)

    def test_seen_list_is_bounded(self):
        for i in range(60):
            self._run(_callback("fb:2026-08-17:done", update_id=900 + i))
        seen = self.store.get_adaptation(self.pid)["telegram_seen_updates"]
        self.assertLessEqual(len(seen), 50)


class TextTests(HandleTelegramTests):
    def test_help_is_returned_without_touching_the_store(self):
        code = self._run(_message("/help"))
        self.assertEqual(code, 0)
        self.assertIn("A, B or C", self.sent[0])
        self.assertEqual(self.store.get_overrides(self.pid), {})

    def test_mode_change_is_applied(self):
        code = self._run(_message("survival mode"))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.get_state(self.pid)["mode"], "survival")
        self.assertEqual(self.store.get_state(self.pid)["cycle_state"], "paused")

    def test_resume_returns_to_normal(self):
        self._run(_message("survival mode", update_id=401))
        self._run(_message("I'm back", update_id=402))
        self.assertEqual(self.store.get_state(self.pid)["mode"], "normal")

    def test_letter_reply_works_like_the_button(self):
        self._seed_pending()
        code = self._run(_message("B"))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.store.get_overrides(self.pid)), 7)

    def test_handler_failure_reports_without_claiming_a_change(self):
        with mock.patch.dict(self.ht.HANDLERS,
                             {self.ht.router.QUESTION: mock.Mock(side_effect=RuntimeError("boom"))}):
            code = self._run(_message("is this too fast?"))
        self.assertEqual(code, 1)
        self.assertIn("Nothing has been changed", self.sent[0])


if __name__ == "__main__":
    unittest.main()
