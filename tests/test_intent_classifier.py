"""Unit tests for intent_classifier.classify().

Mocks gemini_client.call_gemini so no network calls are made.
Run from the fitness-emails dir:  python -m unittest tests.test_intent_classifier
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import intent_classifier  # noqa: E402


def _gemini_returns(payload):
    """Return a patch decorator that makes call_gemini return the given payload (as JSON)."""
    return patch.object(intent_classifier.gemini_client, "call_gemini", return_value=payload)


class ClassifyTests(unittest.TestCase):

    def test_parses_single_intent(self):
        response = json.dumps({"intents": [
            {"intent": "training_feedback", "text": "swap tomorrow's run for easy"}
        ]})
        with _gemini_returns(response):
            result = intent_classifier.classify("swap tomorrow's run for easy")
        self.assertEqual(len(result["intents"]), 1)
        self.assertEqual(result["intents"][0]["intent"], "training_feedback")
        self.assertEqual(result["intents"][0]["text"], "swap tomorrow's run for easy")

    def test_parses_compound_intents(self):
        response = json.dumps({"intents": [
            {"intent": "food_log", "text": "ate 3 eggs and toast"},
            {"intent": "training_feedback", "text": "swap tomorrow's run for easy"},
        ]})
        with _gemini_returns(response):
            result = intent_classifier.classify(
                "ate 3 eggs and toast, swap tomorrow's run for easy"
            )
        intents = [i["intent"] for i in result["intents"]]
        self.assertEqual(intents, ["food_log", "training_feedback"])

    def test_accepts_all_valid_intent_values(self):
        for intent in intent_classifier.VALID_INTENTS:
            response = json.dumps({"intents": [{"intent": intent, "text": "x"}]})
            with _gemini_returns(response):
                result = intent_classifier.classify("x")
            self.assertEqual(result["intents"][0]["intent"], intent)

    def test_raises_on_malformed_json(self):
        with _gemini_returns("not json at all"):
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                intent_classifier.classify("anything")

    def test_raises_on_invalid_intent_value(self):
        response = json.dumps({"intents": [{"intent": "make_coffee", "text": "x"}]})
        with _gemini_returns(response):
            with self.assertRaisesRegex(ValueError, "Invalid intent"):
                intent_classifier.classify("x")

    def test_raises_on_missing_text(self):
        response = json.dumps({"intents": [{"intent": "food_log", "text": ""}]})
        with _gemini_returns(response):
            with self.assertRaisesRegex(ValueError, "missing text"):
                intent_classifier.classify("x")

    def test_raises_on_missing_intents_key(self):
        response = json.dumps({"foo": "bar"})
        with _gemini_returns(response):
            with self.assertRaisesRegex(ValueError, "no intents"):
                intent_classifier.classify("x")

    def test_raises_on_empty_intents_list(self):
        response = json.dumps({"intents": []})
        with _gemini_returns(response):
            with self.assertRaisesRegex(ValueError, "no intents"):
                intent_classifier.classify("x")

    def test_raises_on_non_dict_intent_item(self):
        response = json.dumps({"intents": ["just a string"]})
        with _gemini_returns(response):
            with self.assertRaisesRegex(ValueError, "not a dict"):
                intent_classifier.classify("x")

    def test_propagates_gemini_runtime_error(self):
        with patch.object(
            intent_classifier.gemini_client,
            "call_gemini",
            side_effect=RuntimeError("Gemini API returned HTTP_STATUS:500"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP_STATUS:500"):
                intent_classifier.classify("x")


if __name__ == "__main__":
    unittest.main()
