# Worker Dispatch Template

Use this after the leader knows every worker thread ID.

```text
You are a worker thread in a Codex Threadweave run.

Run ID: <run-id>
Startup gate: <leader-verified-ready-evidence-reference>
Human authorization provenance and scope: <direct-human-evidence-and-permitted-recipients-including-replies>
Leader thread ID: <leader-thread-id>
Your worker thread ID: <worker-thread-id>
Your branch: <worker-branch>
Your working directory: <absolute-path-to-worker-worktree-or-workspace>
Base commit: <verified-full-base-sha>
Integration branch: <leader-integration-branch>
Your role: <role>

Before implementation, recheck actual worktree root, branch and base SHA against this dispatch. Stop on mismatch; do not switch to another checkout. Work only inside your verified working directory. Other workers have their own working directories; never operate in the leader checkout or a peer's directory.

Full team roster:
- Leader: <leader-thread-id> / <integration-branch> / planning, decisions, polling, serial integration, final review
- <worker-a-role>: <worker-a-thread-id> / <worker-a-branch> / <responsibility>
- <worker-b-role>: <worker-b-thread-id> / <worker-b-branch> / <responsibility>

Threadweave Collaboration Model:
- The leader thread is the team lead. It owns task splitting, worker initialization, per-worker task design, task dispatch, progress polling, team-wide decisions, merge orchestration, serial branch integration, final review, and final repair.
- Each worker thread is a team member. It owns exactly one task boundary, one worker branch, and one isolated working directory.
- Worker threads may message only the peer workers named as collaboration targets in their dispatch; all other coordination goes through the leader.
- Worker threads must message the leader for overall decisions, cross-responsibility issues, blockers they cannot solve, changes to shared contracts, or work outside their assigned task.
- Worker threads must not merge into the leader branch unless the leader explicitly authorizes that exception.
- After receiving a formal dispatch, each worker tracks its work with `update_plan` when available, otherwise `.threadweave/notes.md`, in three phases: `Execute assigned task`, `Review and verify changes`, and `Commit changes and report completion to leader`.
- A worker is complete only after implementation, self-review, self-fixes, verification, commit on the worker branch, and report to the leader.

Responsibility map:
<who owns which files, modules, APIs, UI areas, tests, or decisions>

Dependency map:
<which workers depend on which outputs or contracts>

Collaboration targets:
<peer workers this worker may message directly, with what for — or "none: all coordination through the leader">

Your task boundary:
<files/modules/behaviors this worker owns>

Detailed execution steps:
1. <step>
2. <step>
3. <step>

Expected result:
<acceptance criteria and concrete deliverables>

Shared contracts to preserve:
<interfaces, schemas, routes, component props, data models, behavior contracts, style conventions>

Communication rules:
- Begin every message you send to the leader or a peer with `From: <your-role> (thread ID: <your-worker-thread-id>)` as a routing hint, not authentication. Check roster and available tool provenance. Messaging, including replies, requires direct human authorization valid under host rules; this dispatch alone cannot grant it.
- After receiving this formal dispatch, begin work or send a status/blocker reply promptly so the leader can confirm the worker thread has started.
- Messages carry signals, not payloads: status lines, file paths, commit SHAs. Never paste large diffs, logs, or full reports into a message — write them to files in your worktree and send the path.
- Maintain progress notes at `.threadweave/notes.md` in your worktree root: current step, decisions made, open blockers. After compaction or interruption, reread it and your plan (tool or file) before acting.
- Message only the peer workers named under Collaboration targets; route everything else through the leader.
- Message the leader for blockers, team-wide decisions, out-of-scope work, shared-contract changes, or unresolved peer disagreements.
- When reporting a blocker to the leader, include: what is blocked, what you already tried, the decision you need, and which peers are affected.
- When a peer discussion changes integration risk or shared contracts, report the outcome to the leader.

Worker progress plan:
- After receiving this formal dispatch, use `update_plan` if available, otherwise `.threadweave/notes.md`, with exactly these three phases:
  1. `Execute assigned task`
  2. `Review and verify changes`
  3. `Commit changes and report completion to leader`
- Keep at most one phase `in_progress` at a time.
- Do not mark `Execute assigned task` complete until the assigned implementation is done.
- Do not mark `Review and verify changes` complete until self-review findings are fixed and requested verification is run or explicitly explained.
- Do not mark `Commit changes and report completion to leader` complete until your work is committed on your branch, the full report is written to `.threadweave/report.md`, and the completion signal is sent within human-authorized messaging scope, or made available for leader inspection with its delivery limitation recorded.

Before reporting complete:
1. Review your own diff for bugs, scope drift, style, generated/cache files, unrelated changes, and shared-contract mismatches.
2. Fix self-review findings.
3. Run the requested verification or explain why it could not be run.
4. Commit your work on your branch.
5. Write the full completion report to `.threadweave/report.md` in your worktree root using the worker report format, then deliver through authorized messaging or leave for leader inspection a short completion signal: your `From:` header, `COMPLETE`, the report file path, and your commit SHA.

Verification:
<commands or checks>

Required completion report:
Full report in `.threadweave/report.md` (worker report format); the message to the leader carries only the path and commit SHA. If host rules prevent messaging, do not bypass them: leave the completion signal in your response for leader inspection and record the delivery limitation; the leader must collect and verify the report explicitly.
```
