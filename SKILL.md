---
name: claude-orchestrator
description: Delegate bounded reviews, investigations, or implementations to Claude Code with Opus 5.5, Opus 5, or Fable 5.1, then supervise, resume, and verify the work. Use when the user asks Codex to orchestrate Claude, Opus, or Fable agents.
---

# Claude orchestration

Use the existing Claude Code subscription login. Codex owns task scope, review, tests, and integration. Claude runs as an external CLI worker, not a native Codex subagent. Do not switch to API billing, buy extra usage, or substitute models to work around limits.

Runner: `~/.codex/skills/claude-orchestrator/scripts/claude_task.py` (Python 3, no dependencies).
CLI: `~/.local/bin/claude`. Check `claude auth status` for authentication; if needed, use `claude auth login` and let the user complete sign-in. Never read or print credential files. Do not use `--bare`: it excludes subscription OAuth.

## Scope and model

Honor the requested model: `--model opus-5.5` pins `claude-opus-5-5`; `--model opus-5` pins `claude-opus-5`; `--model fable-5.1` pins `claude-fable-5-1`. If the user delegates to Claude without specifying, use Opus 5 for ordinary bounded coding/review work, Fable 5.1 for substantial investigations or complex implementations. Name the selected model in the progress update. Opus 5.5 requires Claude Code 2.1.280 or later. Verify account availability when needed; a local model option alone does not prove access.

Pass requested effort explicitly with `--effort low|medium|high|xhigh|max`. `max` and `xhigh` are distinct; do not substitute. Omission retains the CLI's default behavior. Resume preserves the pinned model and effort, rejecting a different explicit effort. A legacy task without a saved effort can pin one on resume. The runner removes an inherited `CLAUDE_CODE_EFFORT_LEVEL` only when forwarding an explicit effort, so it cannot override that request.

Receipts distinguish the requested effort from runtime evidence: `effort_confirmation: requested_only` does not prove the effective backend setting. Do not infer effort from a model's self-report or thinking-token count. The runner verifies actual assistant model IDs separately from synthetic provider-error messages. Missing identity, a real model mismatch, and provider errors remain failures even when the CLI envelope says success. Inspect and disclose the returned identity; do not count a fallback as the requested review.

Inspect git status and applicable repository policy. Include relevant AGENTS.md, CLAUDE.md, operations rules, exact allowed files, task context, and acceptance criteria in the prompt. The runner deliberately uses Claude safe mode: automatic CLAUDE.md, hooks, skills, plugins, memory, and other customizations are disabled; pass needed instructions explicitly. Managed policies remain in force. Do not include secrets.

Review mode provides only Read, Glob, and Grep tools. It cannot run shell commands or edit files. Codex can supply output from authorized read-only commands when needed. Claude's restricted mode confines file tools to the working directory. This is tool-level isolation, not a separate container or OS sandbox.

Write mode requires an existing clean linked Git worktree. Create one using repository conventions and a `codex/` branch unless otherwise instructed. Do not use this runner to change the user's main checkout. Worktrees start from committed source; explicitly account for relevant uncommitted work. One writer per worktree. Follow repository requirements for local databases and services. The runner never creates, deletes, commits, merges, or pushes worktrees.

## Run and supervise

Write a private prompt file outside the repository. Examples:

```bash
python3 ~/.codex/skills/claude-orchestrator/scripts/claude_task.py run --cwd /absolute/checkout --mode review --model opus-5 --prompt-file /absolute/prompt.txt
python3 ~/.codex/skills/claude-orchestrator/scripts/claude_task.py run --cwd /absolute/checkout --mode review --model opus-5.5 --effort xhigh --prompt-file /absolute/prompt.txt
python3 ~/.codex/skills/claude-orchestrator/scripts/claude_task.py run --cwd /absolute/worktree --mode write --model fable-5.1 --prompt-file /absolute/prompt.txt
```

Write mode allows Read, Glob, Grep, Edit, and Write. Bash is absent unless the invocation supplies a scoped permission such as `--allow 'Bash(bun run typecheck)'`. Grant only commands needed for the authorized assignment. Permission prompts are denied automatically; do not bypass permissions to resolve a blocker. MCP, browser, scheduling, and agent tools are not part of the allowed toolset.

Default limits are 12 agentic turns and 900 seconds; choose `--max-turns` and `--timeout` for the bounded assignment. Include the relevant files/source locations, exact question, acceptance criteria, and stopping point; do not respond to repeated exhaustion by only raising limits. Run via the host's ongoing command/session mechanism, keep the session alive, and poll at reasonable intervals. Independent workers may run concurrently in separate worktrees when authorized by the user and current delegation policy. They share the account's usage limits.

For repositories with the installed worktree lifecycle, pass `--owner ACTUAL_PARENT_CHAT_ID` (or inherit `CODEX_THREAD_ID`). The runner verifies registered ownership, records a worker hold before launch, prevents overlapping writers, and releases the hold only after its owned process group exits. Primary-checkout review mode remains read-only. Unregistered existing worktrees are protected and cannot be adopted; create an owned checkout instead. Registry contention is retried for up to five seconds. Inspect `cleanup_status` separately from the worker result: `hold_release_pending` means process exit was confirmed but metadata release failed. The runner returns nonzero for this condition while preserving the model result; reconcile and release the exact recorded hold instead of rerunning a completed review. `process_exit_unconfirmed` remains a process-cleanup failure. A worker hold ending does not archive the checkout: the parent verifies results and records archive/retention at phase closeout. Do not add a duplicate manual hold for this same worker. Safe mode disables hooks, so these explicit checks matter.

The first output contains a task UUID. Receipts and private per-attempt logs live in `~/.codex/claude-tasks/<task-id>/`. The latest state is in `task.json`; each completed attempt also saves `receipt.json` alongside its logs. Receipts include requested effort, observed model IDs, identity status, completion, and configured tool restrictions. These restrictions are CLI tool isolation, not an OS sandbox; check event logs for actual tool use and permission denials.

```bash
python3 ~/.codex/skills/claude-orchestrator/scripts/claude_task.py status TASK_UUID
python3 ~/.codex/skills/claude-orchestrator/scripts/claude_task.py result TASK_UUID
python3 ~/.codex/skills/claude-orchestrator/scripts/claude_task.py resume TASK_UUID --prompt-file /absolute/followup.txt
python3 ~/.codex/skills/claude-orchestrator/scripts/claude_task.py list
```

Resume the owning task for follow-ups to its existing edits. After a failed attempt, inspect the exact process, receipt, tool activity, and diff; supply `--recovery-note "Reconciled result and changed condition"`. Three consecutive failed attempts block further resume: narrow/replan the assignment before starting a new task, preserving prior work and evidence. There are no automatic provider retries. Authentication/rate-limit errors follow the user's applicable account-routing policy; timeouts, permission failures, turn limits, and real model mismatches do not justify an account switch. A new account requires a new task after reconciliation; resume pins the saved account profile. Older receipts lacking that profile also require reconciliation and a new task. Re-supply needed scoped shell grants on each invocation. Logs from earlier attempts remain available. `status` returns the compact receipt. `result` adds at most 12,000 characters of the latest response text, omitting tool payloads and internal thinking. Use `--max-chars` (100..100000) only when needed; full events and stderr stay in private logs. `list` shows the 20 most recently updated tasks by default (`--limit` up to 200). Inspect permission denials, model IDs, result subtype, and errors. `exited` means Claude reported success, not that Codex verified the work. Turn-limit exhaustion, missing result, timeout, and nonzero exits are recorded as failures.

Launch temporary workers through the applicable resource supervisor with a finite deadline. Cancel using Ctrl-C/SIGTERM on the runner's execution session; it terminates its exact child process group. Normal completion also releases remaining owned descendants; an unconfirmed exit retains the hold and reports a cleanup failure. A forcible kill may leave a stale running receipt: inspect actual processes before recovery, never blindly retry uncertain work. Per-task locks prevent concurrent resumes.

Review the diff and run focused checks before integrating under repository branch policy. Preserve worktrees until changes are accounted for. Report the selected and observed models, what Claude did, what Codex verified, and remaining limitations. Usage/cost metadata are estimates, not proof of billing mode or remaining subscription allowance.

Official references: https://code.claude.com/docs/en/headless and https://code.claude.com/docs/en/model-config
