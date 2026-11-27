"""Which agy model ids take --effort."""

import unittest

from cli_subagents.runtimes.antigravity import takes_effort


class TakesEffortTests(unittest.TestCase):
    def test_default_and_gemini_base_names_take_effort(self):
        self.assertTrue(takes_effort(None))
        self.assertTrue(takes_effort(""))
        self.assertTrue(takes_effort("gemini-4-argon"))

    def test_ids_with_a_level_do_not(self):
        for model in ("gemini-4-hydrogen-high", "gemini-4-argon-medium", "gpt-oss-120b-medium", "gemini-4-hydrogen-low"):
            self.assertFalse(takes_effort(model), model)

    def test_third_party_models_do_not(self):
        for model in ("claude-sonnet-4-6", "claude-opus-4-6-thinking"):
            self.assertFalse(takes_effort(model), model)


if __name__ == "__main__":
    unittest.main()
