---
description: Hand a task to a subagent and bring back the result
argument-hint: <task description>
---

Delegate this task through CLI Subagents:

$ARGUMENTS

1. Turn the request into a self-contained task: the goal, which files or folders matter, what must not be touched, and how to check the result. The subagent can't see this conversation.
2. Start the `cli-subagents:cli-subagents-claude` agent with your Agent tool, in the background (use `cli-subagents-codex`, `cli-subagents-antigravity` or `cli-subagents-opencode` when the user asked for Codex, Gemini or OpenCode). Set its description to `<model> · <access> · <task in a few words>`. Begin its prompt with `cwd=<absolute path of the current project>` and `access=read` for analysis or review, or `access=edit` when files should change, then the task.
3. When the agent finishes, its final message is the worker's result. If it relays a question from the worker, answer with SendMessage to that agent.
4. Check the work (read changed files, run the relevant tests) and report what the subagent did.

If those agent types aren't available, use the MCP tools instead: `subagent_start` with the task, `cwd` and `access`, then `subagent_wait` with the job ID, answering any `open_questions` with `subagent_message`.
