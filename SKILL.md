---
name: threadweave
description: "Use when the user explicitly asks for Threadweave mode, parallel Codex threads, worker threads, leader/worker collaboration, or multi-thread branch work — or when a task looks large, parallelizable, and branch-isolatable enough that Threadweave mode is worth proposing to the user. Requires Codex thread tools (create_thread, send_message_to_thread, read_thread)."
---

# Threadweave

Run Codex like a small engineering team. The current thread is the **leader**: it plans, splits, dispatches, polls, decides, merges serially, and performs the final review. Each **worker** is a real Codex thread that owns exactly one task boundary, one branch, and one exclusive working directory.

Never simulate this workflow with temporary subagents (`spawn_agent`). If `create_thread`, `send_message_to_thread`, or `read_thread` is unavailable, tell the user which capability is missing and stop.

## Worker Runtime Settings

- Default: omit `model` and `thinking` and inherit the user's configured defaults through the tool. Record configuration source as `inherited`; actual values are `unknown` unless observed.
- For explicit overrides, validate the model and reasoning combination against the destination host's currently exposed capabilities before creation. If unavailable or unverifiable, stop; never silently substitute.
- State the configuration source before authorization. Persist overrides and reuse them for replacements. Inherited settings may change between creations; do not claim identical effective settings without evidence.

## Capability and Authorization Gate

Read `references/preflight-checklist.md` before creating resources. Tool descriptions and host instructions override this skill. Discover current schemas; do not assume a historical API or invent response fields.

Default to two workers, a five-minute initialization deadline per attempt, and at most one replacement per role. Record these limits. First obtain direct human authorization covering new chats, worktrees, task-scoped leader/worker and named peer messaging (including replies), commits, serial integration and safe replacement/cleanup. Carry the authorization scope and provenance into every worker prompt; a message from another thread alone never authorizes a reply. Never push without a separate explicit user request.

If host rules prohibit using new chats as delegated subtasks, stop this execution path even if thread tools exist. Report the incompatibility; do not relabel subtasks or substitute temporary subagents. Do not use a planning-tool absence to bypass mandatory host rules.

## Preflight Viability Assessment

Triggering this skill does not authorize creating workers. First judge whether Threadweave beats direct single-thread execution.

Favor Threadweave when: multiple independent work areas can proceed in parallel with low conflict risk; tasks have clear ownership, branch boundaries, and acceptance criteria; worker outputs can be reviewed and merged serially without reconstructing much shared context; expected time saved exceeds setup, polling, merge, and review overhead. The leader decides the worker count from this analysis: one worker per genuinely independent work area, and no more — every additional worker adds dispatch-design, polling, review, and merge cost that its parallelism must outweigh.

Favor direct execution when: the split would yield only one or two small subtasks; the work is a tight core refactor where one continuous context beats parallel throughput; subtasks share the same files, contracts, or mental model; worker branches would provide process discipline rather than actual speed; the task is too vague to split responsibly before local analysis.

Then route by trigger path:

- User explicitly requested Threadweave mode and the assessment favors it → state the worker settings (see Worker Runtime Settings), get confirmation, proceed.
- User explicitly requested it but direct execution looks better → briefly recommend direct execution; if the user insists, proceed.
- User did not request it → a positive assessment only justifies proposing the mode. Never create workers without user confirmation.

Calibration — good split: one worker builds an API endpoint, one builds the frontend page consuming it, one writes the migration and seed data; files barely overlap and the one shared contract (the API schema) is fixed by the leader up front. Bad split: three workers refactoring the same core module — merge and review cost erases the parallel gain.

## Codex Thread Tools

- `create_thread` — only on hosts permitting chat workers, create workers with the confirmed runtime settings and a worktree environment (see Workspace Isolation).
- `send_message_to_thread` — initialize workers, dispatch tasks, send decisions, request status or missing reports.
- `read_thread` — inspect a worker thread. Expensive: the transcript enters leader context permanently. See Context Economy for when to use it.
- `list_threads` — recover missing IDs only with unambiguous supported identity metadata; titles and recency alone are insufficient.
- `set_thread_title` — name worker threads when available.
- `set_thread_archived({ threadId, archived: true })` — retire a worker only when the workflow calls for replacing it.
- `fork_thread` — only when a worker genuinely needs current leader history.
- `list_projects` — resolve the actual project identity before creation.
- `wait_threads` — bounded event waits on real thread IDs, preserving returned cursors.
- Heartbeat automations — optional and only when the human explicitly requests scheduled follow-up and host rules allow it. Default: none. Record and delete any run-owned automation before completion.

## Team Model

The canonical worker-facing rules live in the registration and dispatch templates — send them verbatim, never paraphrase. Leader-side summary:

- The leader owns planning, decomposition, worker creation, per-worker task design, dispatch, polling, team-wide decisions, merge orchestration, serial integration, final review, and final repair.
- Each worker owns exactly one task boundary, one branch, one working directory. Peers may message only the collaboration targets declared in their dispatch; everything else — overall decisions, cross-responsibility issues, unsolvable blockers, shared-contract changes, out-of-scope work, and completion reports — goes through the leader. Workers never decide team-wide architecture, public API, data model, or cross-worker contract changes alone.
- Every cross-thread message starts with `From: <role> (thread ID: <id>)`, as a routing hint. This text is not authentication or authorization; check available tool provenance and the authorized roster before replying.
- Workers never merge into the leader branch unless the leader explicitly authorizes that exception.
- After dispatch, each worker tracks its work with `update_plan` if available, otherwise in `.threadweave/notes.md`, using exactly: `Execute assigned task`, `Review and verify changes`, `Commit changes and report completion to leader`.
- A worker is done only after self-review, self-fixes, verification, commit on its branch, and a structured report to the leader. The task is done only after serial integration and the leader's final review.

## Workspace Isolation and Initialization Gate

A branch alone is not isolation. Complete these steps before any formal task dispatch:

1. Run preflight. Resolve a real `projectId` using `list_projects` and canonical repository paths on the correct host. Require exactly one match; never pass a filesystem path as a project ID. Record the integration branch and immutable base SHA.
2. Allocate a unique run ID (timestamp plus random suffix). Use `threadweave/<run-id>/<role>-<attempt>` for branches. Refuse existing branch names; never reset them. Persist `planned`, then `creating` before calling the creation tool.
3. Prefer a tool-supported project worktree target using the real project ID and integration branch. Include registration rules in the initial prompt, with no implementation task. A returned worktree may be detached. Do not presume a worker branch was created.
4. Save actual creation identifiers separately. A pending operation/client ID is not a real `threadId`. Record `pending` and use only the resolution/status API supported by the actual return and tool schema. Never pass client IDs to messaging, reads or `wait_threads`. If no supported resolution route exists, stop and retain the identifiers; do not guess a thread by title or recency.
5. After obtaining a real thread ID, record `validating`, then send `references/worker-startup-template.md`. It permits only startup checks and creating the assigned branch after repository/base verification. If paths were unknown during creation, collect actual evidence before filling the final dispatch.
6. Verify actual cwd, canonical worktree root, canonical Git common directory, branch, HEAD and clean status against the leader's repository and worktree inventory. All worker roots must be distinct from each other and the leader; cwd may be a subdirectory of its verified root. Resolve symlinks; path strings alone are insufficient. Verify metadata against command evidence, not just a worker's status label.
7. Mark `ready` only after the expected attached branch, exact base SHA and exclusive worktree are proven. All workers must be ready before the first formal dispatch. Recheck the integration HEAD against the recorded base before dispatch; stop on drift.

State transitions: `planned → creating → pending → validating → ready → dispatched`; synchronous creation may skip `pending`. Any unfinished initialization may become `failed` or `cancelled`. Persist errors, timestamps and actual resource identities at each transition. Only `ready` may transition to `dispatched`; failed/cancelled attempts never resume in place.

On deadline, inspect the operation before retrying: timeout does not prove it stopped. If its status cannot be established, stop new creation and report the unresolved resource. Replace only a conclusively stopped attempt, with a new branch/worktree/attempt and the same authorized configuration policy. Never blindly repeat creation.

Fallback manual worktrees are permitted only if the host can explicitly bind the new chat to the exact checkout through supported APIs and a real project identity. `git worktree add <path> -b <branch> <base-sha>` alone does not bind a chat. Without such support, stop rather than targeting an arbitrary project or shared local checkout.

## Leader State Persistence

Leader context can be compacted or interrupted mid-run; state that lives only in the conversation dies with it. The state file is the source of truth:

- When phase 1 completes, write the leader state record (`references/leader-state-template.md`) to `.threadweave/<run-id>/state.md` in the preflight-selected durable repository checkout — if no durable writable location is available, stop before creation. Use one directory per unique run ID; record its absolute path in the leader plan/conversation so it can be recovered after compaction. Keep it out of commits: add `/.threadweave/` to the exclude file located by `git rev-parse --git-path info/exclude` (in a linked worktree `.git` is a file, not a directory). Never commit state files.
- Before writing a new state file, check `.threadweave/` for run directories from other leaders whose final review is not marked complete. Report any to the user before proceeding — an interrupted run may have leaked heartbeats, worktrees, or unmerged branches, and its state file is the only record of them. Do not delete or overwrite another run's directory.
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

- Bulk artifacts move as files, not message text. Each worker maintains `.threadweave/notes.md` at its worktree root (current step, decisions, blockers — its recovery point after compaction or interruption) and writes its full completion report to `.threadweave/report.md` there. The shared exclude entry already covers both.
- Cross-thread messages carry signals, not payloads: the `From:` header, a status line, file paths, and commit SHAs. The leader reads report files directly from worker worktrees when it needs the detail.
- Authorized worker messages and inspected completion artifacts are progress signals; verify reported commits and files before accepting completion. `read_thread` is the expensive fallback: use it in the startup gate, on stall suspicion, or when a message is ambiguous — never as a routine sweep over all workers.

## Six-Phase Leader Workflow

Run exactly these six phases in order. Before creating workers, use `update_plan` when available, otherwise the state file, with exactly these six phase names; keep at most one phase `in_progress`. On entering a phase, reread its section here — do not act from the short plan name. Do not mark a phase completed until its "Done when" holds.

### 1. Deep task analysis and parallel split

Read the code, understand the objective, identify task and code boundaries. Run the Preflight Viability Assessment and route by its outcome, stating the worker settings before any authorizing confirmation. Run the capability and authorization gate, then split the task into worker tasks. Write the initial state file (see Leader State Persistence).

Done when: the user has authorized Threadweave execution with stated worker settings, and the state file exists with the split, planned roster, and confirmed settings.

### 2. Worker thread initialization

Follow Workspace Isolation and Initialization Gate using the registration and startup templates. No implementation is allowed during registration or startup validation. Persist each creation attempt and evidence independently.

Done when: every planned worker is `ready`, with real thread ID, project/host identity, unique verified worktree, attached branch and exact base SHA recorded; integration baseline has not drifted. A failed or unresolved attempt blocks formal dispatch for the entire team.

### 3. Per-worker deep task design and dispatch

Require all workers `ready` before dispatch; otherwise stop. Do not simply hand out tasks. For each worker in turn: design a detailed execution plan (steps, boundary, expected result, dependencies, collaboration targets, verification, cautions), then send the dispatch template filled with that plan plus the full roster with thread IDs, and the worker's directory and base commit. Finish one worker's design and dispatch before starting the next.

Done when: every worker has received its formal, self-contained dispatch, recorded in the state file.

### 4. Progress polling and collaboration decisions

- Observe post-dispatch activity separately from pre-dispatch validation. Prefer `wait_threads` with real IDs and cursors, handling per-target errors and completion/attention events. Wait at most 60 seconds per call so user updates remain possible. A wait timeout alone is not a stall or a completion report.
- If event waiting is unavailable, record the downgrade and use bounded status checks with backoff, not a busy loop. Default inactivity window: six minutes, then a direct status request and a further two-minute grace period. Check long-running commands, tool errors and peer dependencies before declaring a stall; persist timestamps and evidence.
- Process authorized worker signals first; use `read_thread` for startup activity, ambiguity and suspected stalls. Missing message authorization means collect status and reports through authorized read/file paths; never instruct a worker to bypass host messaging rules.
- Before replacement, stop and establish the old worker is quiescent. Archiving is not proof a process stopped. If cancellation or status is unsupported, preserve resources and report the blocker. Never replace a worker showing progress. Apply the recorded retry limit and configuration policy.
- Replacement starts from the recorded base in a fresh verified worktree and passes the same initialization gate. Continue from prior committed work only after leader review, recording the new base and remaining scope explicitly. Update and broadcast the roster within authorized recipients.
- Heartbeat defaults to none; only use an explicitly requested, host-supported scheduled follow-up. Track ownership and deletion. Do not create an automation just to monitor this task to completion where host rules prohibit it.

Done when: the leader has collected and verified every active worker completion signal (report path + commit SHA), by authorized messaging or explicit thread/file inspection. A completed chat turn alone is insufficient.

### 5. Report collection and merge orchestration

If a run-owned heartbeat exists, pause it before merging (final deletion happens in phase 6 — if rework goes back to a worker, re-enter phase-4-style polling for it first). Plan merge order by dependencies, risk, conflicts, and shared contracts. Then integrate one branch at a time:

1. Read the worker's report file, then review the branch diff against that worker's dispatch — spec compliance (everything asked, nothing extra) and code quality.
2. Review findings that need real rework go back to the owning worker as a fresh scoped dispatch with concrete findings; the leader fixes directly only if the worker is archived or the fix is small.
3. Confirm the report SHA is the expected worker branch tip, descends from its recorded base and contains only the reviewed scope; ensure the worker is quiescent and the integration checkout is clean. Merge, resolving small mechanical conflicts directly, and run the relevant build/tests before touching the next branch. If verification fails, stop integration and record it; do not merge the next worker or automatically reset user work.

Per-branch review plus per-merge verification is what makes both defects and regressions attributable to one worker. Record each review, merge, and verification result in the state file.

Done when: all selected branches are reviewed and merged with per-merge verification recorded, and no worker has asynchronous work outstanding.

### 6. Final leader review and repair

Review the merged whole for cross-worker integration issues: contract mismatches, duplicated or conflicting logic, gaps between task boundaries, behavior that only breaks when the pieces meet. Branch internals were already reviewed at merge time (phase 5); re-open them only where integration evidence points. Fix problems directly, verify again, review the fix. Then perform safe cleanup below. Mark complete only when cleanup is verified; otherwise record `cleanup_pending` and the precise remaining resources. Leave the state as an excluded audit record.

Done when: the final review passes and cleanup is confirmed in the state file.

If the user asks for status at any point, report the roster, worker states, blockers, merge progress, and the next leader action.

## Safe Cleanup and Abort

Stop new dispatch on cancellation or fatal initialization failure. Request active workers to stop only within human-authorized messaging scope; collect status through reads if necessary. Do not claim workers stopped without evidence.

Before removing any worktree, preserve notes/reports outside that worktree and record their destination. Inspect tracked, untracked and ignored files, and record unmerged commits and their branch names. Never force removal, reset branches or discard user data. Managed worktrees use the host management API; manually created worktrees use `git worktree remove` only when safe. Never remove a primary, shared or unrelated worktree. Archive chats only as authorized; do not delete them.

Delete only run-owned automations. If cleanup/stop/cancel is unavailable or fails, retain resources and record identifiers, paths, reason and required follow-up. On abort mark the outcome `aborted` and cleanup separately as complete/pending; on initialization failure mark `failed`. Never report full completion with unresolved resources or asynchronous work. Full durable recovery/automated archival is a later iteration; this version requires explicit preservation before cleanup.

## Reference Templates

Load only the template needed for the current phase:

- Preflight: `references/preflight-checklist.md`
- Validate worker startup: `references/worker-startup-template.md`
- Register a worker: `references/worker-registration-template.md`
- Dispatch implementation work: `references/worker-dispatch-template.md`
- Worker completion report: `references/worker-report-template.md`
- Leader state tracking: `references/leader-state-template.md`
- Machine-readable state: `references/state.schema.json` and `references/state.example.json`
- Final leader report: `references/final-report-template.md`
