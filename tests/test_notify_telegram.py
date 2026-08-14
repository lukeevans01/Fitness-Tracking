"""Tests for outbound Telegram notifications.

No network: the curl call is stubbed. Covers the configuration gate, message splitting,
and the guarantee that a Telegram failure never raises into the caller (the email is the
source of truth and must not be undone by a notification problem).

Run from the fitness-emails dir:  python3 -m unittest tests.test_notify_telegram
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

import notify_telegram as nt  # noqa: E402

CONFIGURED = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "12345"}


class _FakeCompleted:
    def __init__(self, stdout, stderr=""):
        self.stdout = stdout
        self.stderr = stderr


def _ok_run(*args, **kwargs):
    return _FakeCompleted('{"ok":true}\nHTTP_STATUS:200\n')


def _fail_run(*args, **kwargs):
    return _FakeCompleted('{"ok":false,"description":"chat not found"}\nHTTP_STATUS:400\n')


class ConfigurationTests(unittest.TestCase):
    def test_not_configured_when_env_missing(self):
        with mock.patch.dict(nt.os.environ, {}, clear=True):
            self.assertFalse(nt.is_configured())

    def test_not_configured_with_only_token(self):
        with mock.patch.dict(nt.os.environ, {"TELEGRAM_BOT_TOKEN": "t"}, clear=True):
            self.assertFalse(nt.is_configured())

    def test_configured_with_both(self):
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True):
            self.assertTrue(nt.is_configured())

    def test_send_skipped_and_no_curl_when_unconfigured(self):
        with mock.patch.dict(nt.os.environ, {}, clear=True), \
             mock.patch.object(nt.subprocess, "run") as run:
            self.assertFalse(nt.send_message("hello"))
            run.assert_not_called()


class SplitMessageTests(unittest.TestCase):
    def test_short_message_is_one_chunk(self):
        self.assertEqual(nt.split_message("hello"), ["hello"])

    def test_empty_message_yields_nothing(self):
        self.assertEqual(nt.split_message(""), [])

    def test_splits_on_line_boundaries(self):
        text = "\n".join(["aaaa", "bbbb", "cccc"])
        chunks = nt.split_message(text, limit=9)
        self.assertEqual(chunks, ["aaaa\nbbbb", "cccc"])

    def test_every_chunk_within_limit(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
        for chunk in nt.split_message(text, limit=500):
            self.assertLessEqual(len(chunk), 500)

    def test_single_overlong_line_is_hard_split(self):
        chunks = nt.split_message("y" * 25, limit=10)
        self.assertTrue(all(len(c) <= 10 for c in chunks))
        self.assertEqual("".join(chunks), "y" * 25)

    def test_no_content_lost_when_splitting(self):
        text = "\n".join(f"row {i}" for i in range(60))
        chunks = nt.split_message(text, limit=40)
        self.assertEqual("\n".join(chunks), text)

    def test_default_chunk_stays_under_telegram_hard_limit(self):
        self.assertLess(nt.CHUNK_CHARS, nt.MAX_MESSAGE_CHARS)


class SendMessageTests(unittest.TestCase):
    def test_successful_send_returns_true(self):
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=_ok_run) as run:
            self.assertTrue(nt.send_message("today's session"))
            self.assertEqual(run.call_count, 1)

    def test_body_is_passed_on_stdin_not_argv(self):
        """The token is in the URL; the body goes over stdin so long text is safe."""
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=_ok_run) as run:
            nt.send_message("body text")
            args, kwargs = run.call_args
            self.assertIn("@-", args[0])
            self.assertIn("body text", kwargs["input"])

    def test_failed_send_returns_false_without_raising(self):
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=_fail_run):
            self.assertFalse(nt.send_message("nope"))

    def test_curl_exception_is_swallowed(self):
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=OSError("no curl")):
            self.assertFalse(nt.send_message("boom"))

    def test_timeout_is_swallowed(self):
        err = nt.subprocess.TimeoutExpired(cmd="curl", timeout=1)
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=err):
            self.assertFalse(nt.send_message("boom"))

    def test_long_message_sends_multiple_parts_with_counter(self):
        text = "\n".join(f"line {i} " + "z" * 60 for i in range(200))
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=_ok_run) as run:
            self.assertTrue(nt.send_message(text))
            self.assertGreater(run.call_count, 1)
            first_body = run.call_args_list[0][1]["input"]
            self.assertIn("(1/", first_body)

    def test_stops_after_first_failure(self):
        text = "\n".join(f"line {i} " + "z" * 60 for i in range(200))
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=_fail_run) as run:
            self.assertFalse(nt.send_message(text))
            self.assertEqual(run.call_count, 1)

    def test_notify_prefixes_the_subject(self):
        with mock.patch.dict(nt.os.environ, CONFIGURED, clear=True), \
             mock.patch.object(nt.subprocess, "run", side_effect=_ok_run) as run:
            nt.notify("Fitness plan — Fri 14 Aug", "Session details here")
            body = run.call_args[1]["input"]
            self.assertIn("Fitness plan", body)
            self.assertIn("Session details here", body)


if __name__ == "__main__":
    unittest.main()
