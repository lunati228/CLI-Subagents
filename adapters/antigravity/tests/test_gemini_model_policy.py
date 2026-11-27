import ast
import unittest
from pathlib import Path

from reviewer_client import REVIEWER_EFFORT, REVIEWER_MODEL


SERVER_PATH = Path(__file__).resolve().parents[1] / "mcp_server.py"


def server_tree():
    return ast.parse(SERVER_PATH.read_text(encoding="utf-8"))


def server_constant(name):
    for node in server_tree().body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"missing server constant: {name}")


def server_docstring(name):
    for node in server_tree().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_docstring(node)
    raise AssertionError(f"missing server function: {name}")


class GeminiModelPolicyTests(unittest.TestCase):
    def test_delegate_uses_gemini_4_hydrogen_high(self):
        self.assertEqual(server_constant("GEMINI_MODEL"), "gemini-4-hydrogen-high")
        self.assertEqual(server_constant("GEMINI_EFFORT"), "high")

    def test_delegate_tool_descriptions_name_gemini_4_hydrogen_high(self):
        self.assertIn(
            "Gemini 4 Hydrogen High at high effort",
            server_docstring("antigravity_delegate_gemini"),
        )
        self.assertIn(
            "Gemini 4 Hydrogen High at high effort",
            server_docstring("antigravity_delegate_gemini_async"),
        )

    def test_fail_closed_reviewer_uses_gemini_4_hydrogen_high(self):
        self.assertEqual(REVIEWER_MODEL, "gemini-4-hydrogen-high")
        self.assertEqual(REVIEWER_EFFORT, "high")


if __name__ == "__main__":
    unittest.main()
