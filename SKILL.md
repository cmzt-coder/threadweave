---
name: thread-team
description: "Use when the user explicitly asks for a thread team, parallel Codex threads, worker threads, leader/worker collaboration, or multi-thread branch work — or when a task looks large, parallelizable, and branch-isolatable enough that thread-team mode is worth proposing to the user. Requires Codex thread tools (create_thread, send_message_to_thread, read_thread)."
---

# Thread Team

Run Codex like a small engineering team. The current thread is the **leader**: it plans, splits, dispatches, polls, decides, merges serially, and performs the final review. Each **worker** is a real Codex thread that owns exactly one task boundary, one branch, and one exclusive working directory.

Never simulate this workflow with temporary subagents (`spawn_agent`). If `create_thread`, `send_message_to_thread`, or `read_thread` is unavailable, tell the user which capability is missing and stop.

## Worker Runtime Settings

Single source of truth for worker settings — every other section refers here.

- Default: model `gpt-5.4`, thinking `high`. The user may override either.
- Before the user confirms thread-team execution, state plainly: "Worker threads will be created with model `gpt-5.4` and thinking `high` unless you request different worker settings." The user's confirmation then authorizes those settings.
- Include the confirmed `model` and `thinking` in every worker `create_thread` call. Never omit them and rely on tool defaults.
- Record the confirmed settings in the leader state file so replacement workers are created identically.

## Preflight Viability Assessment

Triggering this skill does not authorize creating workers. First judge whether a thread team beats direct single-thread execution.

Favor a thread team when: multiple independent work areas can proceed in parallel with low conflict risk; tasks have clear ownership, branch boundaries, and acceptance criteria; worker outputs can be reviewed and merged serially without reconstructing much shared context; expected time saved exceeds setup, polling, merge, and review overhead. The leader decides the worker count from this analysis: one worker per genuinely independent work area, and no more — every additional worker adds dispatch-design, polling, review, and merge cost that its parallelism must outweigh.

Favor direct execution when: the split would yield only one or two small subtasks; the work is a tight core refactor where one continuous context beats parallel throughput; subtasks share the same files, contracts, or mental model; worker branches would provide process discipline rather than actual speed; the task is too vague to split responsibly before local analysis.

Then route by trigger path:

- User explicitly requested thread-team mode and the assessment favors it → state the worker settings (see Worker Runtime Settings), get confirmation, proceed.
- User explicitly requested it but direct execution looks better → briefly recommend direct execution; if the user insists, proceed.
- User did not request it → a positive assessment only justifies proposing the mode. Never create workers without user confirmation.

Calibration — good split: one worker builds an API endpoint, one builds the frontend page consuming it, one writes the migration and seed data; files barely overlap and the one shared contract (the API schema) is fixed by the leader up front. Bad split: three workers refactoring the same core module — merge and review cost erases the parallel gain.

## Codex Thread Tools

- `create_thread` — create workers, with the confirmed runtime settings and a worktree environment (see Workspace Isolation).
- `send_message_to_thread` — initialize workers, dispatch tasks, send decisions, request status or missing reports.
- `read_thread` — inspect a worker thread. Expensive: the transcript enters leader context permanently. See Context Economy for when to use it.
- `list_threads` — recover worker thread IDs only when the state file has gaps.
- `set_thread_title` — name worker threads when available.
- `set_thread_archived({ threadId, archived: true })` — retire a worker only when the workflow calls for replacing it.
- `fork_thread` — only when a worker genuinely needs current leader history.
- Heartbeat automations — delayed polling instead of busy-waiting. Search for the automation tool before creating, updating, or deleting one. Every heartbeat this workflow creates must be deleted before the workflow ends; a leaked heartbeat outlives the task and wakes the thread forever.

## Team Model

The canonical worker-facing rules live in the registration and dispatch templates — send them verbatim, never paraphrase. Leader-side summary:

- The leader owns planning, decomposition, worker creation, per-worker task design, dispatch, polling, team-wide decisions, merge orchestration, serial integration, final review, and final repair.
- Each worker owns exactly one task boundary, one branch, one working directory. Peers may message only the collaboration targets declared in their dispatch; everything else — overall decisions, cross-responsibility issues, unsolvable blockers, shared-contract changes, out-of-scope work, and completion reports — goes through the leader. Workers never decide team-wide architecture, public API, data model, or cross-worker contract changes alone.
- Every cross-thread message starts with `From: <role> (thread ID: <id>)`, so the receiver can verify the sender against the roster and reply to that thread ID.
- Workers never merge into the leader branch unless the leader explicitly authorizes that exception.
- After dispatch, each worker tracks its work with `update_plan` using exactly: `Execute assigned task`, `Review and verify changes`, `Commit changes and report completion to leader`.
- A worker is done only after self-review, self-fixes, verification, commit on its branch, and a structured report to the leader. The task is done only after serial integration and the leader's final review.

## Workspace Isolation

A branch alone does not isolate workers. Two threads sharing one checkout overwrite each other's working tree, corrupt every diff, and silently mix changes across branches — the most common way a thread team fails. Before any dispatch, guarantee each worker an exclusive working directory:

- Preferred: create isolation at thread-creation time — `create_thread({ prompt, target: { type: "project", projectId: <workspace-root-or-project-id>, environment: { type: "worktree", startingState: { type: "branch", branchName: <integration-branch> } } }, model: <confirmed>, thinking: <confirmed> })` — so the worker starts inside its own worktree. Record each worker's worktree path in the state file and repeat it in that worker's dispatch.
- Fallback (only if `create_thread` cannot provide a worktree environment): `git worktree add <path> -b <worker-branch> <base-commit>` per worker, then create the worker with a project target rooted at that path and a local environment. Tell the worker its exact working directory and base commit in the dispatch, and during the startup gate confirm from its actual command activity that it executes inside that directory.
- Never let two workers share a working directory. If exclusive directories cannot be arranged, do not run thread-team mode — fall back to direct execution and tell the user why.
- After integration, remove leader-created worktrees (`git worktree remove <path>`).

## Leader State Persistence

Leader context can be compacted or interrupted mid-run; state that lives only in the conversation dies with it. The state file is the source of truth:

- When phase 1 completes, write the leader state record (`references/leader-state-template.md`) to `.thread-team/<leader-thread-id>/state.md` under the repo root — one directory per run, keyed by leader thread ID, so successive or concurrent runs never overwrite each other and the leader can always re-derive its own state path after compaction from its own thread ID. Keep it out of commits: add `.thread-team/` to the exclude file located by `git rev-parse --git-path info/exclude` (in a linked worktree `.git` is a file, not a directory). Never commit state files.
- Before writing a new state file, check `.thread-team/` for run directories from other leaders whose final review is not marked complete. Report any to the user before proceeding — an interrupted run may have leaked heartbeats, worktrees, or unmerged branches, and its state file is the only record of them. Do not delete or overwrite another run's directory.
- Update the file immediately after every state change: worker created, worktree assigned, dispatch sent, decision made, worker replaced, report received, branch merged, heartbeat scheduled or deleted.
- After compaction, interruption, or any suspicion of stale context, reread the state file before acting. Use `list_threads` only to fill gaps the file cannot answer.

## Dispatch Clarity

Workers may reason less strongly and hold less context than the leader; compensate with precise, self-contained dispatches:

- Never send vague goals ("refactor this area", "make this cleaner") without concrete boundaries.
- Every dispatch spells out: exact task scope, working directory and base commit, files/packages owned and not owned, expected behavior, acceptance criteria, verification commands, commit requirements, and report format.
- For fragile behavior, name the invariants to preserve: event order, message text, public API, transcript format, permission behavior, concurrency behavior, other user-visible contracts.
- Include leader-known context (prior analysis, worker reports, failed attempts, user decisions) directly in the dispatch instead of assuming the worker can recover it.

## Context Economy

Everything pasted into a cross-thread message — and every `read_thread` result — stays resident in the reading thread's context for the rest of the run. The leader's context is the team's scarcest resource; protect it:

- Bulk artifacts move as files, not message text. Each worker maintains `.thread-team/notes.md` at its worktree root (current step, decisions, blockers — its recovery point after compaction or interruption) and writes its full completion report to `.thread-team/report.md` there. The shared exclude entry already covers both.
- Cross-thread messages carry signals, not payloads: the `From:` header, a status line, file paths, and commit SHAs. The leader reads report files directly from worker worktrees when it needs the detail.
- Worker messages are the authoritative progress signal. `read_thread` is the expensive fallback: use it in the startup gate, on stall suspicion, or when a message is ambiguous — never as a routine sweep over all workers.

## Six-Phase Leader Workflow

Run exactly these six phases in order. Before creating workers, call `update_plan` with exactly these six phase names; keep at most one phase `in_progress`. On entering a phase, reread its section here — do not act from the short plan name. Do not mark a phase completed until its "Done when" holds.

### 1. Deep task analysis and parallel split

Read the code, understand the objective, identify task and code boundaries. Run the Preflight Viability Assessment and route by its outcome, stating the worker settings before any authorizing confirmation. Only after that analysis, split the task into worker tasks. Write the initial state file (see Leader State Persistence).

Done when: the user has authorized thread-team execution with stated worker settings, and the state file exists with the split, planned roster, and confirmed settings.

### 2. Worker thread initialization

Create each worker with the confirmed settings and an exclusive working directory (see Workspace Isolation); record branch, base commit, and directory in the state file. Send each worker the registration template — this phase is identity and rules only, no task content.

Done when: every worker has an exclusive working directory recorded in the state file and has received the registration message.

### 3. Per-worker deep task design and dispatch

Do not simply hand out tasks. For each worker in turn: design a detailed execution plan (steps, boundary, expected result, dependencies, collaboration targets, verification, cautions), then send the dispatch template filled with that plan plus the full roster with thread IDs, and the worker's directory and base commit. Finish one worker's design and dispatch before starting the next.

Done when: every worker has received its formal, self-contained dispatch, recorded in the state file.

### 4. Progress polling and collaboration decisions

- Startup gate: poll every worker with `read_thread` until each shows a post-dispatch response (acknowledgement with intent, tool or command activity, implementation, status, blocker, or report). If one worker stays silent past a reasonable window while others have started: archive it, create a replacement with fresh identity/branch/worktree and the same settings, resend the same dispatch, update the state file, and broadcast the replacement to active workers (old thread ID retired, new ID and role). Never replace a worker that has started and shows observable progress.
- Heartbeat passes: once all workers have started, schedule a two-minute heartbeat and end the leader turn instead of busy-waiting. On each wake: process queued worker messages first (blockers, decisions, completion signals — messages are the authoritative signal, per Context Economy), make and record decisions, sync them to affected workers; `read_thread` only the workers that are silent beyond expectation or whose last message is ambiguous. Keep or reschedule the heartbeat if reports are still outstanding.
- Mid-task stall: a started worker with no new activity across roughly three passes, not inside a long-running command and not waiting on a peer, gets a direct status request; if it stays silent through the next pass, decide — re-dispatch the remaining work to it, replace it (same archive/replace/broadcast procedure), or pull the work into the leader — and record the decision.
- Replacement baseline: a replacement re-executes the full task from the integration branch in a fresh worktree. Only when the stuck worker left committed work the leader has inspected and verified may the replacement continue from that commit, with the dispatch stating exactly what is done and what remains.

Done when: every active worker has sent its completion signal (report file path + commit SHA).

### 5. Report collection and merge orchestration

Pause the report-waiting heartbeat before merging (final deletion happens in phase 6 — if rework goes back to a worker, re-enter phase-4-style polling for it first). Plan merge order by dependencies, risk, conflicts, and shared contracts. Then integrate one branch at a time:

1. Read the worker's report file, then review the branch diff against that worker's dispatch — spec compliance (everything asked, nothing extra) and code quality.
2. Review findings that need real rework go back to the owning worker as a fresh scoped dispatch with concrete findings; the leader fixes directly only if the worker is archived or the fix is small.
3. Merge, resolving small mechanical conflicts directly, and run the relevant build/tests before touching the next branch.

Per-branch review plus per-merge verification is what makes both defects and regressions attributable to one worker. Record each review, merge, and verification result in the state file.

Done when: all selected branches are reviewed and merged with per-merge verification recorded, and no worker has asynchronous work outstanding.

### 6. Final leader review and repair

Review the merged whole for cross-worker integration issues: contract mismatches, duplicated or conflicting logic, gaps between task boundaries, behavior that only breaks when the pieces meet. Branch internals were already reviewed at merge time (phase 5); re-open them only where integration evidence points. Fix problems directly, verify again, review the fix. Then confirm cleanup: every heartbeat this workflow created is deleted, leader-created worktrees are removed, and the run's state file is marked complete (leave the directory as an excluded audit record).

Done when: the final review passes and cleanup is confirmed in the state file.

If the user asks for status at any point, report the roster, worker states, blockers, merge progress, and the next leader action.

## Abort

If the user cancels the run, or the leader concludes it cannot continue, tear the team down — never just stop responding:

1. Message every active worker to stop and commit any coherent work-in-progress on its branch.
2. Delete every heartbeat automation this workflow created.
3. Archive all worker threads.
4. Report which branches hold unmerged commits, then remove leader-created worktrees the user does not want to keep.
5. Mark the state file `aborted` with a one-line reason and leave it as the audit record.

## Reference Templates

Load only the template needed for the current phase:

- Register a worker: `references/worker-registration-template.md`
- Dispatch implementation work: `references/worker-dispatch-template.md`
- Worker completion report: `references/worker-report-template.md`
- Leader state tracking: `references/leader-state-template.md`
- Final leader report: `references/final-report-template.md`
