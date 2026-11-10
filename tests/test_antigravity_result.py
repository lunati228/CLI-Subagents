"""An Antigravity job's result is its final message, not every message of the run (seen live 2026-11-04)."""

import unittest

from cli_subagents.runtimes.antigravity import AntigravityParser


def said(parser, index, text):
    parser.feed({"event": "step_update", "step_update": {"step_type": "agent_response", "step_index": index,
                                                          "text_delta": text}})


class AntigravityResultTests(unittest.TestCase):
    def finish(self, steps, response):
        parser = AntigravityParser()
        for index, text in enumerate(steps, 1):
            said(parser, index * 2, text)
            parser.feed({"event": "step_update", "step_update": {"step_type": "tool", "step_index": index * 2 + 1,
                                                                  "tool_name": "read"}})
        parser.feed({"event": "result", "result": {"status": "SUCCESS", "response": response}})
        return parser.finish(0, "").text

    def test_a_response_that_repeats_every_message_becomes_the_last_one(self):
        steps = ["I am starting Step 1.", "Step 1 done; reading the tests.", "### Findings\n\n1. A bug."]
        self.assertEqual(self.finish(steps, "\n".join(steps)), steps[-1])
        self.assertEqual(self.finish(steps, "".join(steps)), steps[-1])

    def test_other_responses_stay_as_they_are(self):
        steps = ["I am starting.", "Here is the answer."]
        for response in ("A summary agy wrote itself.", "Here is the answer.", "I am starting.\nExtra.\nHere is the answer.",
                         "Preface. I am starting.\nHere is the answer."):
            with self.subTest(response=response):
                self.assertEqual(self.finish(steps, response), response)
        self.assertEqual(self.finish(["Only one message."], "Only one message."), "Only one message.")
