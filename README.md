# Claude Orchestrator for Codex

A custom local Codex skill that delegates bounded tasks to the Claude Code CLI, preserves task receipts, checks the observed model identity, and lets Codex review the result.

## Install

Requires local terminal and filesystem access, Python 3, Git, and an authenticated Claude Code installation. The runner targets macOS/Linux (POSIX process groups and `fcntl`); native Windows is not supported by this version.

If these destination paths already exist, inspect and back them up before installing. For a new installation:

```sh
mkdir -p ~/.codex/skills ~/.agents/skills
git clone https://github.com/tadamsadlinkt/claude-orchestrator.git ~/.codex/skills/claude-orchestrator
ln -s ~/.codex/skills/claude-orchestrator ~/.agents/skills/claude-orchestrator
python3 -B ~/.codex/skills/claude-orchestrator/scripts/claude_task.py --help
```

The runner expects the Claude executable at `~/.local/bin/claude`. Install Claude Code following its current official documentation at https://code.claude.com/docs/en/setup and authenticate on this machine using your intended subscription account. If it is installed elsewhere, inspect that executable and create a symlink only if the expected path is absent. Do not overwrite an existing executable.

```sh
~/.local/bin/claude auth login
~/.local/bin/claude auth status
```

Do not copy another machine's credentials or task history. This repository does not include Claude Code, model access, or a subscription. Check actual CLI support for the flags used by the runner before launching work. The bundled model aliases and minimum version requirements reflect the source skill; installation does not establish model availability. Never silently substitute a model.

Codex discovers user skills through `~/.agents/skills`, including symlinks. Restart the desktop app or start a new local chat if needed. See https://learn.chatgpt.com/docs/build-skills.

## Ask Codex to install it

Paste this in a local Codex chat:

> Install the Claude Orchestrator skill from https://github.com/tadamsadlinkt/claude-orchestrator. Read the README and SKILL.md first. Preserve existing installations and unrelated settings. Install at ~/.codex/skills/claude-orchestrator and expose it through a symlink at ~/.agents/skills/claude-orchestrator. Check Python 3, Git, and Claude Code; install missing prerequisites using current official instructions where permitted. The runner expects ~/.local/bin/claude. Help me complete subscription sign-in interactively without reading or printing credentials. Verify the runner's --help and run only ClassificationTests from tests/test_worker_support.py with bytecode writing disabled. Check required CLI flags against actual CLI help. Do not launch a live model task during installation. Report installed paths, versions, authentication status, checks, and blockers. Follow this machine's policies; do not bypass organization restrictions or install AdLinkt-specific hooks/configuration.

## Usage

Ask Codex to use `claude-orchestrator`, specifying the model and effort you want. Read `SKILL.md` for runner commands, read-only review mode, write-mode worktree requirements, time/turn limits, resuming, and cleanup.

Task receipts and logs remain local in `~/.codex/claude-tasks/`. They can contain prompts and private source material; never commit them. Tool restrictions are not an operating-system sandbox. Existing repository policies still apply.

## Offline validation

The self-contained classification tests require no model call or lifecycle installation:

```sh
cd ~/.codex/skills/claude-orchestrator/tests
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_worker_support.ClassificationTests -v
```

Other bundled tests exercise fake subprocesses, Git worktrees, and process cleanup. `LeaseTests` additionally requires the separately installed AdLinkt worktree-lifecycle runtime. That optional host integration is not bundled or installed here; when no matching local lifecycle registry exists, the runner operates without it. Follow the host's worker supervision and repository policies.

This distribution contains the original skill, two runner modules, UI metadata, and tests. No credentials, private account routing policies, task logs, or repository data are included.
