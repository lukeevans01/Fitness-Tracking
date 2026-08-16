"""Tests for Telegram update routing.

Pure logic, no network. Leans adversarial on the authorisation checks: anyone can find a
bot by its username and message it, so an update from an unexpected chat must never reach a
handler.

Run from the fitness-emails dir:  python3 -m unittest tests.test_telegram_router
"""

import sys
import unittest
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import telegram_router as tr  # noqa: E402

CHAT = "12345"
OTHER_CHAT = "99999"


def _message(text, chat_id=CHAT, update_id=1, message_id=7, key="message"):
    return {"update_id": update_id,
            key: {"message_id": message_id, "chat": {"id": int(chat_id)}, "text": text}}


def _callback(data, chat_id=CHAT, update_id=2, callback_id="cb1", message_id=7):
    return {"update_id": update_id,
            "callback_query": {"id": callback_id, "data": data,
                               "message": {"message_id": message_id,
                                           "chat": {"id": int(chat_id)}}}}


class AuthorisationTests(unittest.TestCase):
    def test_unknown_chat_is_rejected(self):
        action = tr.route(_message("A", chat_id=OTHER_CHAT), CHAT)
        self.assertEqual(action.kind, tr.UNAUTHORISED)

    def test_unknown_chat_rejected_for_callbacks_too(self):
        action = tr.route(_callback("wk:B", chat_id=OTHER_CHAT), CHAT)
        self.assertEqual(action.kind, tr.UNAUTHORISED)

    def test_missing_expectation_is_closed_not_open(self):
        """Unconfigured must fail shut, or a fresh deploy would accept anyone."""
        for expected in (None, ""):
            self.assertEqual(tr.route(_message("A"), expected).kind, tr.UNAUTHORISED)

    def test_authorised_chat_passes(self):
        self.assertEqual(tr.route(_message("A"), CHAT).kind, tr.WEEK_CHOICE)

    def test_chat_id_compares_as_string_or_int(self):
        self.assertEqual(tr.route(_message("A"), int(CHAT)).kind, tr.WEEK_CHOICE)


class MalformedUpdateTests(unittest.TestCase):
    def test_non_dict_is_ignored(self):
        for bad in ("nope", 42, None, []):
            self.assertEqual(tr.route(bad, CHAT).kind, tr.IGNORE)

    def test_update_without_message_or_callback_is_ignored(self):
        self.assertEqual(tr.route({"update_id": 3}, CHAT).kind, tr.UNAUTHORISED)

    def test_message_without_text_is_ignored(self):
        update = {"update_id": 4, "message": {"message_id": 1, "chat": {"id": int(CHAT)}}}
        self.assertEqual(tr.route(update, CHAT).kind, tr.IGNORE)

    def test_blank_text_is_ignored(self):
        self.assertEqual(tr.route(_message("   "), CHAT).kind, tr.IGNORE)

    def test_unrecognised_callback_is_ignored_but_keeps_the_callback_id(self):
        action = tr.route(_callback("garbage:xyz"), CHAT)
        self.assertEqual(action.kind, tr.IGNORE)
        # Still needed so the spinner can be stopped.
        self.assertEqual(action.callback_id, "cb1")


class CallbackTests(unittest.TestCase):
    def test_week_choice_button(self):
        action = tr.route(_callback("wk:B"), CHAT)
        self.assertEqual(action.kind, tr.WEEK_CHOICE)
        self.assertEqual(action.letter, "B")
        self.assertEqual(action.callback_id, "cb1")
        self.assertEqual(action.message_id, 7)

    def test_all_three_letters(self):
        for letter in ("A", "B", "C"):
            self.assertEqual(tr.route(_callback(f"wk:{letter}"), CHAT).letter, letter)

    def test_invalid_letter_is_ignored(self):
        self.assertEqual(tr.route(_callback("wk:D"), CHAT).kind, tr.IGNORE)

    def test_session_feedback_button(self):
        action = tr.route(_callback("fb:2026-08-17:done"), CHAT)
        self.assertEqual(action.kind, tr.SESSION_FEEDBACK)
        self.assertEqual(action.iso_date, "2026-08-17")
        self.assertEqual(action.feedback, "done")

    def test_all_feedback_kinds(self):
        for kind in sorted(tr.FEEDBACK_KINDS):
            action = tr.route(_callback(f"fb:2026-08-17:{kind}"), CHAT)
            self.assertEqual(action.feedback, kind)

    def test_bad_feedback_date_is_ignored(self):
        self.assertEqual(tr.route(_callback("fb:17-08-2026:done"), CHAT).kind, tr.IGNORE)

    def test_unknown_feedback_kind_is_ignored(self):
        self.assertEqual(tr.route(_callback("fb:2026-08-17:brilliant"), CHAT).kind, tr.IGNORE)

    def test_callback_data_stays_inside_telegrams_limit(self):
        for data in ("wk:B", "fb:2026-08-17:done", "fb:2026-08-17:hard"):
            self.assertLessEqual(len(data.encode()), 64)


class TextRoutingTests(unittest.TestCase):
    def test_letter_reply_picks_the_week(self):
        for text in ("B", "b", " B ", "B."):
            action = tr.route(_message(text), CHAT)
            self.assertEqual(action.kind, tr.WEEK_CHOICE, text)
            self.assertEqual(action.letter, "B")

    def test_help_commands(self):
        for text in ("/help", "help", "/start", "commands"):
            self.assertEqual(tr.route(_message(text), CHAT).kind, tr.HELP, text)

    def test_survival_mode_commands(self):
        for text in ("survival mode", "pause training", "the baby arrived"):
            action = tr.route(_message(text), CHAT)
            self.assertEqual(action.kind, tr.MODE_CHANGE, text)
            self.assertEqual(action.meta["mode"], "survival")

    def test_resume_commands(self):
        for text in ("I'm back", "im back", "resume training"):
            action = tr.route(_message(text), CHAT)
            self.assertEqual(action.kind, tr.MODE_CHANGE, text)
            self.assertEqual(action.meta["mode"], "normal")

    def test_pause_everything(self):
        action = tr.route(_message("pause"), CHAT)
        self.assertEqual(action.meta["mode"], "paused")

    def test_question_mark_means_advice(self):
        action = tr.route(_message("is 5:20/km too quick for easy runs?"), CHAT)
        self.assertEqual(action.kind, tr.QUESTION)

    def test_food_words_mean_a_log(self):
        for text in ("porridge and eggs for breakfast", "had chicken and rice for lunch"):
            self.assertEqual(tr.route(_message(text), CHAT).kind, tr.FOOD_LOG, text)

    def test_anything_else_is_training_feedback(self):
        action = tr.route(_message("knees are sore, drop the squats tomorrow"), CHAT)
        self.assertEqual(action.kind, tr.TRAINING_FEEDBACK)

    def test_a_question_about_food_prefers_the_question(self):
        """A trailing '?' is the stronger signal; it must not be logged as a meal."""
        action = tr.route(_message("should I eat more protein?"), CHAT)
        self.assertEqual(action.kind, tr.QUESTION)

    def test_edited_messages_are_routed_too(self):
        action = tr.route(_message("B", key="edited_message"), CHAT)
        self.assertEqual(action.kind, tr.WEEK_CHOICE)

    def test_update_id_is_carried_for_deduplication(self):
        self.assertEqual(tr.route(_message("B", update_id=42), CHAT).update_id, 42)


class HelpTextTests(unittest.TestCase):
    def test_mentions_the_main_commands(self):
        text = tr.help_text()
        for expected in ("A, B or C", "survival mode", "pause"):
            self.assertIn(expected, text)

    def test_fits_a_single_telegram_message(self):
        self.assertLess(len(tr.help_text()), 4096)


if __name__ == "__main__":
    unittest.main()
