# Threadweave

English | [简体中文](README.zh-CN.md)

**Run one Codex conversation as a small engineering team.**

Threadweave is a Codex skill that turns the current thread into a **leader** — it plans, splits the task, and dispatches work — and creates real Codex **worker threads** that each own exactly one task boundary, one branch, and one exclusive git worktree. The leader polls, unblocks, reviews and merges branches one at a time, then does a final integration review before handing the result back.

This can only exist on Codex, because only Codex has first-class threads: `create_thread` spawns a real session with its own model settings, and threads talk to each other through `send_message_to_thread`. Workers are therefore real threads, not throwaway subagents — they keep their own context, run with their own settings (inheriting user-configured defaults unless explicitly overridden), can be messaged mid-flight, and can be archived and replaced if they stall. The skill refuses to fake the workflow with `spawn_agent`: if the thread tools aren't available, it names the missing capability and stops.

## Built around the ways parallel agents actually fail

Most of this skill is not "how to spawn threads." It's guardrails — one per failure mode.

**Shared checkouts corrupt everything.** Two threads in one working directory overwrite each other's tree and silently mix changes across branches — the most common way a Threadweave run dies. Every worker gets an exclusive git worktree, created at thread-creation time when possible. If exclusive directories can't be arranged, the skill won't run in team mode at all.

**The leader's context is the team's scarcest resource.** Every `read_thread` transcript and every pasted diff stays resident in the leader's context for the rest of the run. So cross-thread messages carry signals — a status line, file paths, commit SHAs — and payloads move as files: each worker keeps progress notes in `.threadweave/notes.md` and writes its full completion report to `.threadweave/report.md` in its worktree. `read_thread` is the expensive fallback for startup checks, suspected stalls, and ambiguous messages — never a routine sweep.

**Long runs outlive the conversation.** Leader context can be compacted or interrupted mid-run, and state that lives only in the conversation dies with it. Everything — roster, worktrees, dispatches, decisions, replacements, merges, heartbeats — is journaled to `.threadweave/<run-id>/state.md` immediately after every change, and the leader rereads it before acting after any interruption. The directory is git-excluded, never committed, and left behind as an audit record.

**Waiting must be bounded.** Prefer `wait_threads` on real thread IDs, preserving cursors and handling per-target errors. Fall back to bounded status checks with backoff. Heartbeat defaults to none; scheduled follow-up requires an explicit human request and host support.

**Workers stall.** After six minutes without activity, request status and allow a two-minute grace period, checking long commands and dependencies. Establish that the old worker stopped before replacement; timeout or archival is not proof. Allow one replacement per role, with a new attempt, branch and worktree passing the initialization gate again.

**Big-bang merges make defects unattributable.** Integration is serial: read the worker's report, review the branch diff against its dispatch (spec compliance first, then code quality), send real rework back to the owning worker, merge, run the build and tests — only then touch the next branch. After all merges, the leader reviews the merged whole for cross-worker issues: contract mismatches, duplicated logic, seams that only break when the pieces meet.

**Vague dispatches produce vague work.** Workers may hold less context than the leader, so every dispatch is self-contained: exact scope, working directory and base commit, owned and not-owned files, acceptance criteria, verification commands, invariants to preserve, and the required report format — plus the full roster with thread IDs, to assist message routing, not authenticate senders (`From: <role> (thread ID: <id>)`).

## How a run works

The leader executes exactly six phases, each with an explicit exit condition:

1. **Analysis & split** — read the code, run the viability assessment, split the task, and get your confirmation. Validate real project identity, Git baseline, capabilities and authorization, and state the configuration source before creation. The state file is written here.
2. **Worker initialization** — resolve real thread IDs and verify actual directories, repository identity, attached branches and base SHA. All workers must be ready before any formal dispatch; registration and startup perform no implementation.
3. **Design & dispatch** — design a full execution plan per worker, then send the formal dispatch. One worker at a time; design always precedes dispatch.
4. **Polling & decisions** — observe post-dispatch activity, prefer event waits and use bounded checks when necessary: process blockers, make team-wide calls, sync decisions to affected workers, handle stalls and replacements.
5. **Review & serial merge** — per-branch review against the dispatch, rework loops back to the owning worker, merge and verify one branch at a time.
6. **Final review & cleanup** — integration-level review of the merged whole, direct fixes, then confirmed teardown: heartbeats deleted, leader-created worktrees removed, state marked complete only after verified cleanup, otherwise cleanup pending.

Each worker follows the same three-step arc — execute, self-review and verify, commit and report — and may only message the peers named in its dispatch; everything else routes through the leader. Workers never rewrite shared contracts or merge into the leader's branch on their own.

Ask for status at any point and you get the roster, worker states, blockers, merge progress, and the leader's next action. Cancel at any point and the team is torn down deliberately — workers commit coherent work-in-progress to their branches, heartbeats are deleted, chats archived where authorized and supported, unmerged work preserved, and outcome marked `aborted` with a separate cleanup status. Never force-remove worktrees containing results.

## When it helps — and when it doesn't

Triggering the skill doesn't create workers. The leader first judges whether a team actually beats one thread, and it will tell you when it doesn't.

A team wins when independent work areas can run in parallel with low conflict risk, ownership and acceptance criteria are clear, and the wall-clock time saved exceeds the setup, polling, review, and merge overhead. Worker count follows from the analysis: one worker per genuinely independent area, and no more.

Direct execution wins when the split would yield only one or two small subtasks, the work is a tight core refactor where one continuous context beats parallel throughput, or the subtasks share the same files, contracts, and mental model.

Calibration, straight from the skill: a **good split** is one worker on an API endpoint, one on the frontend page consuming it, one on the migration and seed data — files barely overlap and the one shared contract (the API schema) is fixed by the leader up front. A **bad split** is three workers refactoring the same core module — merge and review cost erases the parallel gain.

## Quick start

Prerequisites: Codex with real thread tools, Git, and the target repository registered as a Codex project.

1. Install the skill:

```bash
git clone https://github.com/cmzt-coder/threadweave.git "${CODEX_HOME:-$HOME/.codex}/skills/threadweave"
```

2. Restart Codex so the skill is picked up.
3. Register the target repository root as a Codex project.
4. Start a task:

```text
Use $threadweave to split this task across two workers, each with its own worktree and branch, then integrate serially.
```

See the [usage guide](docs/usage-guide.md) for troubleshooting, recovery, and cleanup.

## Install

Clone into your Codex skills directory as `threadweave`:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone https://github.com/cmzt-coder/threadweave.git "${CODEX_HOME:-$HOME/.codex}/skills/threadweave"
```

To update, run `git pull` inside that directory.

Requires a host that permits real chats as workers, project discovery, creation, messaging, thread inspection, exclusive worktrees, Git and write access. Tool availability alone does not authorize delegation. Stop team execution when host rules prohibit chat subtasks. Use file plans without `update_plan`, and bounded status checks without `wait_threads`. Never pass paths as project IDs or pending/client IDs as real thread IDs.

Defaults: two workers, five minutes per initialization attempt, one replacement per role. Require a clean, attached integration checkout with committed HEAD; never automatically stash or commit user changes. Validate explicit model/reasoning overrides on the destination host. Record inherited effective values as unknown when unobservable.

## Usage

Ask for it explicitly:

> Use $threadweave to split this task across a leader thread and collaborating worker threads.

Any mention of Threadweave, parallel Codex threads, worker threads, or leader/worker branch work triggers it. Codex may also propose the mode on its own when a task looks large, parallelizable, and branch-isolatable — but it never creates workers without your confirmation.

Task shapes it handles well:

> Use $threadweave to implement the new billing settings page. Split backend API work, frontend UI work, and tests across workers.

> Use $threadweave to parallelize this migration: one worker handles schema changes, one updates the data access layer, and one updates tests/docs.

Expect one checkpoint up front: confirming the split, new chats/worktrees, configuration source, task-scoped messaging including replies, commits, serial integration and safe replacement/cleanup. After that the run is autonomous; you're pulled in only for blockers that need your call, and you can ask for status or abort at any time.

## Layout

```
threadweave/
├── SKILL.md                             # the leader workflow (what Codex reads)
├── agents/
│   └── openai.yaml                      # display name & default prompt
└── references/                          # loaded per phase; worker-facing ones sent verbatim
    ├── preflight-checklist.md           # environment and authorization gate
    ├── worker-startup-template.md       # startup evidence before dispatch
    ├── worker-registration-template.md  # identity + team rules, pre-dispatch
    ├── worker-dispatch-template.md      # the formal, self-contained task dispatch
    ├── worker-report-template.md        # structured completion report
    ├── leader-state-template.md         # crash-safe run journal
    └── final-report-template.md         # leader's wrap-up to the user
```

At runtime, a run also leaves `.threadweave/` at the repo root (git-excluded): the leader's state journal plus each worker's `notes.md` and `report.md` — the paper trail that makes recovery, replacement, and audit possible.

## Modifying the skill

`SKILL.md` carries only what the leader needs at runtime; reusable prompts and report formats live in `references/` and are loaded per phase — worker-facing templates are sent verbatim, so edit them as contracts, not prose. Prefer small diffs over rewrites to keep template history reviewable, and never let a modified version simulate workers with temporary subagents.

## First iteration and validation

See the [implementation plan](docs/第一轮执行闭环改造方案.md) and [validation record](docs/第一轮执行闭环验证记录.md) for scope, evidence and limitations; the [todo list](docs/项目待办清单.md) tracks full status and next steps. This iteration implements protocols and templates, not an executable controller; local Git checks do not replace a real-thread smoke test.
## Local tests

Run all local smoke suites:

```bash
python3 tests/run_all.py
```

This runs 12 suites and currently passes 188 assertions. These checks cover local Git
primitives, schema/state logic and fake-adapter controller logic; they do not replace a
real worker smoke test.

## Project origin

This project is an original implementation. It only references the design ideas of
`codex-thread-team-main.zip`; it does not include or derive code from that project.

## License

MIT License. See [LICENSE](LICENSE).
