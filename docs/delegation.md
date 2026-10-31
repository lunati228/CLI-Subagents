# Delegation rules

`csa rules` prints a ready-made set of instructions for your main agent.
It tells the agent which models to use, when to try another CLI, and how many helpers to start.
You choose the models. Installing the package or running `csa setup` does not install these rules.

The rules are opt-in because they change how your agent delegates work.
Review them before installing. An installing agent should explain them and ask which models you want.

## Preview and install

```bash
csa rules
csa rules --show --target codex
csa rules --install --target claude --dry-run
csa rules --install --target claude
```

The default target is `claude`. Choose `codex` or `antigravity` for those hosts.
You can also pass the path to an instruction file:

```bash
csa rules --install --target ./AGENTS.md
```

Named targets use these files:

| Target | Instruction file |
|---|---|
| `claude` | `CLAUDE.md` in `CLAUDE_CONFIG_DIR`, or `~/.claude` |
| `codex` | `AGENTS.md` in `CODEX_HOME`, or `~/.codex` |
| `antigravity` | `~/.gemini/GEMINI.md` |

For a file path, `AGENTS.md` selects OpenAI defaults and `GEMINI.md` selects Google defaults.
Other file names select Claude defaults. Explicit `--home-*` values override these defaults.

## Choose models by tier

Each tier has a home model and an optional model to try through another CLI.
The home model must belong to your main agent's vendor.

| Tier | Use for | Default home choice |
|---|---|---|
| low | Lookups, file searches and mechanical edits | Cheapest model |
| medium | Normal implementation, tests and contained fixes | Default model |
| high | Hard debugging, code review and ambiguous problems | Strongest model |
| xhigh | Deep planning and high-impact work | Strongest model |
| max | Large features and the hardest work | Strongest model |

The default home choices are generic words with the target's vendor name.
The agent must resolve them to an installed model. Each try-first choice defaults to `none`.
`none` in the home column tells the agent to do that tier's work itself.

Set home choices with `--home-low`, `--home-medium`, `--home-high`, `--home-xhigh` and `--home-max`.
Set try-first choices with `--try-low`, `--try-medium`, `--try-high`, `--try-xhigh` and `--try-max`.
Quote model text that contains spaces. It can include an effort level.

For example:

```bash
csa rules --show --target claude --home-high "Opus 5.5" --try-low "Gemini 3.8 Flash high" --try-medium "Sol 6.1 high" --try-high "Sol 6.1 xhigh" --try-max none
```

These model names are examples. Choose models available in your CLIs.
The rules tell the agent to resolve labels to model IDs and use separate `model=`, `effort=` and `access=` lines.
Relay option lines come first. The worker task follows them and starts with its depth line.
For UI changes or a request for speed, the agent uses the home model.
If CLI Subagents refuses a start, the agent falls back to the home model for that tier.
The agent does not check usage itself.

When a later step waits for a cross-CLI result, the agent still starts it.
It tells you what waits and suggests the home model as the faster option.
You decide whether to switch for speed.

## Work folders

List each work folder with a separate flag:

```bash
csa rules --show --work-folder "<work-folder>" --work-folder "<another-work-folder>"
```

The rule covers those folders and their children. Work code and data stay with the home vendor at every depth.
It also covers work projects outside the listed folders. The agent asks if it cannot tell whether a project is work.
Personal projects try the other CLI column first, subject to the UI and speed exceptions.

These are instructions for the agent. The command does not enforce a data boundary or change runtime settings.

## Limits and depth

```bash
csa rules --show --main-limit 4 --helper-limit 3
```

The main limit counts home vendor subagents started by the main session.
Their helpers do not count toward it. CLI Subagents controls the other CLI job limits.
Each depth-1 subagent gets the helper limit, including its cross-CLI agents.
Depth-2 agents cannot start helpers. Set `--helper-limit 0` to disable helpers.
The main limit must be at least 1. The helper limit must be at least 0.

The template includes the depth lines to put at the top of every subagent prompt.
It tells the agent to split large tasks and pass any waiting dependency back to its parent.
Worker restrictions on starting jobs still apply.
These limits guide the agent; they do not change the host's own nesting settings.

## Web research

The agent prefers an allowed CLI whose search reaches the sources it needs.
The template uses Reddit access through Codex and Antigravity as an example.
It also picks the research model by importance: a fast low-tier model for quick lookups, a stronger one for important or many-source research.
Antigravity workers find YouTube videos and read their title and description, but they can't watch them or read transcripts.
Work data stays with the home vendor. Reading other projects' code to decide what to adopt goes to the strongest home model.

## Update or remove

Run `--install` again to replace the managed block. Pass all the choices you want to keep.
The command renders from its flags and defaults each time; it does not save model choices separately.
Keep your install command if you want to reuse it after a package update.

```bash
csa rules --install --target claude --home-high "Opus 5.5" --try-medium "Sol 6.1 high"
csa rules --remove --target claude --dry-run
csa rules --remove --target claude
```

The block starts with `<!-- cli-subagents:rules:start -->` and ends with `<!-- cli-subagents:rules:end -->`.
Edits inside it are replaced on the next install.
Your own text outside those markers is never changed, including its bytes and line endings.
Appending a new block adds spacing after your text. Removing a block leaves surrounding spacing intact.

When the instruction file is a symlink (into a dotfiles repository, say), the command changes the file it points to and keeps the link.
Before the first change to an existing file, the command saves `<filename>.csa-rules-backup` next to it.
It keeps that backup on later installs and removals. A new file needs no backup.
Repeated installs with the same values make no change.
Dry runs write no files, backups or folders. Broken or repeated marker pairs are rejected.
UTF-16 files must be saved as UTF-8 first.
