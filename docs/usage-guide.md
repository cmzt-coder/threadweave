# Threadweave Usage Guide

This guide is for first-time users. It covers prerequisites, installation, first run, troubleshooting, and recovery.

## Prerequisites

Threadweave relies on real Codex thread capabilities, not generic subagent orchestration. Confirm that:

- Your Codex client supports `create_thread`, `send_message_to_thread`, `read_thread`, `list_projects`, and `wait_threads`.
- It supports isolated worktrees and Git operations.
- The target repository is registered as a Codex project whose root matches `list_projects`.
- The current task permits you to explicitly request worker threads (some hosts restrict task delegation to subagents).

If a required capability is missing, the skill stops and names it. Do not substitute subagents for real workers.

## Install

```bash
git clone https://github.com/cmzt-coder/threadweave.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/threadweave"
```

Restart Codex so the skill is picked up.

## First run

1. Open the target repository in Codex.
2. Register the repository root as a Codex project. Confirm `list_projects` returns its exact path with `isGitRepository: true`.
3. Start a task and describe the workers, scopes, and verification:

```text
Use $threadweave to split this task across two workers:
- worker A owns <scope A>
- worker B owns <scope B>
Each uses an independent worktree and branch. Integrate serially and run tests.
```

4. The leader runs preflight, creates workers, validates startup reports, and only dispatches after every worker is ready.
5. Review the final report, merge result, and cleanup state.

## Project registration and projectId

Threadweave requires exactly one `list_projects` match for the repository root.

Common mistakes:

- Registering only a parent directory (non-Git project) and reusing its projectId.
- Passing a filesystem path as a projectId.
- Having multiple projects match the same repository.

Register the repository root itself as an independent Codex project.

## Local helper commands

The repository includes a local-only CLI that does not call host APIs:

```bash
python3 scripts/threadweave.py preflight --repo .
python3 scripts/threadweave.py status --repo .
python3 scripts/threadweave.py cleanup-plan --repo . --run <run-id> --observations <file>
```

- `preflight`: checks attached branch, HEAD, and clean status.
- `status`: lists runs under `.threadweave/` and returns the latest incomplete run.
- `cleanup-plan`: prints a cleanup plan without deleting anything.

## Troubleshooting

### `create_thread` is restricted

Some hosts prohibit using `create_thread` for subtasks of the current task. In that case:

- run from a new task/session that the user explicitly requested; or
- use a host that allows real thread tools.

Do not substitute `spawn_agent`/subagents for real workers.

### `list_projects` has no match

Register the repository root as a Codex project. Do not reuse a parent directory's projectId.

### Worker startup validation fails

Check the worker's reported:

- cwd is inside its worktree root;
- Git common directory matches the leader;
- branch matches the assigned branch;
- HEAD exactly equals the base SHA;
- worktree is clean;
- worker root does not overlap the leader or another worker.

The leader rejects dispatch on any mismatch and does not reset or overwrite automatically.

### Cancel or stall

- Cancellation is usually turn-boundary steering; it does not guarantee immediate interruption.
- Wait for the turn to end, the thread to become idle, and verify stop evidence and preserved artifacts.
- After 6 minutes of inactivity the leader requests status; after a 2-minute grace period it may declare a stall.

### Model unavailable

If an explicitly requested model/reasoning is unavailable, creation fails before dispatch; there is no silent fallback.

## State, recovery, and cleanup

- State directory: `.threadweave/<run-id>/`, ignored by `.gitignore`.
- Structured state: `state.json`; reports and notes: `reports/<role>/`.
- Recovery classification: `scripts/recovery.py` returns `skip/recreate/block/revalidate/reuse`.
- Cleanup checks worktree cleanliness, branch merge status, archived reports, and worker idle state.
- Never force-remove an active primary checkout. Switch the thread back to Local first, then use non-force `git worktree remove`.

## Permissions and safety

- Creating threads, worktrees, messages, commits, integration, and pushes require explicit user authorization.
- Default behavior is commit-only; pushing requires an explicit user request.
- Do not commit `.threadweave/`, `.DS_Store`, or local runtime files.
- Do not put tokens, keys, `.env` files, or internal paths into public documentation.

## Limitations

- Real thread tools are required; subagents are not a substitute.
- Some failure paths are still being validated in controlled environments; see `docs/宿主兼容矩阵.md` and `docs/第一轮执行闭环验证记录.md`.
- The current version is `0.1.0`; interfaces and protocols may evolve.
