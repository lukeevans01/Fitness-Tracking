"""Tests for coach_orchestrator.answer_training_question — the on-demand coach.

Gemini is patched: call_gemini is replaced with a stub that captures the prompt and
returns a canned JSON answer. We assert (a) the routine library is injected for strength
questions, (b) it is NOT injected for run questions, (c) the prompt forbids plan changes,
and (d) the prose answer is returned. No network, no override writes.

Run from the fitness-emails dir:  python3 -m unittest tests.test_answer_training_question
"""

import json
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

FITNESS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(FITNESS_DIR))

# Drop any stubs left by test_process_replies before importing the real modules.
for _name in ("training_summary", "weekly_load"):
    mod = sys.modules.get(_name)
    if mod is not None and not hasattr(mod, "__file__"):
        del sys.modules[_name]

import coach_orchestrator  # noqa: E402


class AnswerTrainingQuestionTests(unittest.TestCase):

    def _run(self, question, **kwargs):
        captured = {}

        def fake_call_gemini(prompt, *a, **k):
            captured["prompt"] = prompt
            return json.dumps({"answer": "Keep bench at 3x5 @72kg, RIR 3."})

        with patch.object(coach_orchestrator.gemini_client, "call_gemini", fake_call_gemini):
            answer = coach_orchestrator.answer_training_question(question, **kwargs)
        return answer, captured["prompt"]

    def test_strength_question_injects_routine_library_and_returns_answer(self):
        answer, prompt = self._run("Should I deload bench this week?", domain="strength")
        self.assertEqual(answer, "Keep bench at 3x5 @72kg, RIR 3.")
        self.assertIn("ROUTINE TEMPLATE LIBRARY", prompt)
        # Specialist lifting context is present.
        self.assertIn("lifting coach", prompt.lower())
        # Advice-only guardrail is in the prompt.
        self.assertIn("ADVICE ONLY", prompt)

    def test_run_question_does_not_inject_routine_library(self):
        _, prompt = self._run("Is 5:20/km too quick for easy runs?", domain="run")
        self.assertNotIn("ROUTINE TEMPLATE LIBRARY", prompt)

    def test_unknown_domain_falls_back_to_strength(self):
        _, prompt = self._run("General question", domain="bogus")
        self.assertIn("ROUTINE TEMPLATE LIBRARY", prompt)

    def test_missing_answer_key_raises(self):
        def fake_call_gemini(prompt, *a, **k):
            return json.dumps({"notanswer": "x"})
        with patch.object(coach_orchestrator.gemini_client, "call_gemini", fake_call_gemini):
            with self.assertRaises(ValueError):
                coach_orchestrator.answer_training_question("hi", domain="strength")

    def test_bad_json_raises(self):
        def fake_call_gemini(prompt, *a, **k):
            return "not json"
        with patch.object(coach_orchestrator.gemini_client, "call_gemini", fake_call_gemini):
            with self.assertRaises(ValueError):
                coach_orchestrator.answer_training_question("hi", domain="strength")


if __name__ == "__main__":
    unittest.main()
