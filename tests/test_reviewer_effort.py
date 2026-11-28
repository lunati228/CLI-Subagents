"""An optional `effort` per reviewer: parsed and checked in the config, passed to claude and codex."""

import unittest
from unittest.mock import patch

from cli_subagents import config as cfg
from cli_subagents.agy_review import context as gate_context
from cli_subagents.agy_review import reviewer


class ConfigTests(unittest.TestCase):
    def entries(self, *items):
        return cfg._reviewer_entries("antigravity", list(items))

    def test_effort_is_optional_and_lowercased(self):
        haiku, luna, hydrogen = self.entries(
            {"provider": "claude", "model": "haiku", "effort": "Medium"},
            {"provider": "codex", "model": "gpt-6-luna", "account": "codex-2", "effort": "medium"},
            {"provider": "antigravity", "model": "gemini-4-hydrogen-medium"},
        )
        self.assertEqual((haiku.effort, luna.effort, hydrogen.effort), ("medium", "medium", None))
        self.assertEqual(luna.account, "codex-2")

    def test_antigravity_reviewers_take_the_level_from_the_model_id(self):
        with self.assertRaisesRegex(ValueError, "model ID"):
            self.entries({"provider": "antigravity", "model": "gemini-4-hydrogen-low", "effort": "medium"})

    def test_unknown_effort_is_refused(self):
        with self.assertRaisesRegex(ValueError, "effort must be one of"):
            self.entries({"provider": "codex", "effort": "extreme"})

    def test_effort_survives_a_round_trip(self):
        lines = cfg._reviewer_lines("antigravity", self.entries({"provider": "codex", "effort": "medium"}))
        self.assertIn('effort = "medium"', lines)
        self.assertEqual(cfg._reviewer_lines("antigravity", self.entries({"provider": "codex"})),
                         ["", "[[antigravity.reviewers]]", 'provider = "codex"'])


class CommandTests(unittest.TestCase):
    def test_codex_uses_the_configured_effort_and_low_by_default(self):
        configured = reviewer.codex_argv({"command": ["codex"], "effort": "medium"})
        default = reviewer.codex_argv({"command": ["codex"]})
        self.assertIn('model_reasoning_effort="medium"', configured)
        self.assertNotIn('model_reasoning_effort="low"', configured)
        self.assertIn('model_reasoning_effort="low"', default)

    def test_claude_gets_effort_only_when_configured(self):
        answer = '{"result": "{\\"decision\\": \\"allow\\", \\"risk\\": \\"low\\", \\"reason\\": \\"ok\\"}"}'
        seen = []

        def fake_run(argv, **_kwargs):
            seen.append(argv)
            return answer

        with patch.object(reviewer, "_run", fake_run):
            for config in ({"command": ["claude"], "model": "haiku", "effort": "medium"},
                           {"command": ["claude"], "model": "haiku"}):
                try:
                    reviewer.ask_claude(config, "prompt")
                except reviewer.ReviewError:
                    pass  # only the command line matters here
        with_effort, without = seen
        self.assertEqual(with_effort[with_effort.index("--effort") + 1], "medium")
        self.assertNotIn("--effort", without)


class GateContextTests(unittest.TestCase):
    def test_gate_refuses_an_effort_that_is_not_a_level(self):
        self.assertIn("medium", gate_context.EFFORTS)
        self.assertEqual(set(gate_context.EFFORTS), set(cfg.REVIEW_EFFORTS))
        base = {"version": gate_context.VERSION, "access": "edit", "workspaces": ["C:/work"],
                "reviewers": [{"kind": "codex", "command": ["codex"], "effort": 'low"; rm -rf /'}]}
        with self.assertRaisesRegex(gate_context.ContextError, "reviewer.effort"):
            gate_context.validate(base)


if __name__ == "__main__":
    unittest.main()
