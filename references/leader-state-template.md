# Leader State Template

Use this compact record while coordinating a Threadweave run.

Persist to `.threadweave/<run-id>/state.md` in the preflight-selected durable location, never solely inside a disposable worker checkout. Record the absolute path in the leader plan/conversation. Keep runtime files out of Git using `/.threadweave/` in the exclude file resolved by `git rev-parse --git-path info/exclude`. Reread before recovery; inspect incomplete runs without overwriting them.

```md
Threadweave leader state.

Run ID:
Absolute state path:
Project ID / host ID:
Canonical repository root / common directory:
Integration base SHA:
Preflight evidence and required/optional capabilities:
Human authorization provenance / messaging scope:
Limits: two workers / five-minute initialization per attempt / one replacement per role
Leader thread ID:
Integration branch:
Repo path:
User objective:
Worker runtime settings:
- Source: inherited / explicit override
- Requested model / thinking: omitted or exact overrides
- Observed effective model / thinking: exact values or unknown
- Host validation evidence for overrides:

Initialization attempts (one record per role/attempt):
- Role / attempt / expected branch / base SHA:
- State: planned/creating/pending/validating/ready/dispatched/failed/cancelled
- Creation identifiers: actual operation/client fields, separate from real threadId
- Real threadId / supported resolver / last status:
- Created at / deadline / last observation:
- Canonical cwd / worktree root / common directory:
- Actual branch / HEAD / clean status / worktree inventory:
- Leader validation evidence / dispatch timestamp:
- Errors / cancellation evidence / unresolved resources:

Team roster:
| Role | Thread ID | Branch | Working directory | Responsibility | Startup | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Leader | <id> | <branch> | <leader-checkout-path> | Plan, decide, poll, merge, final review | n/a | active |
| <worker> | <id> | <branch> | <worktree-path> | <scope> | planned/creating/pending/validating/ready/failed/cancelled | dispatched/running/blocked/reported/integrated/archived-or-n-a |

Retired workers:
- <old worker>: archived, replaced by <new worker>, reason <startup stuck/no post-dispatch response/etc.>

Dependency map:
- <dependency>

Decision log:
- <decision>

Polling state:
- Phase 4 mode: post-dispatch-observation/event-wait/bounded-status/complete
- Heartbeat automation: <id, or none / deleted>
- Last heartbeat:
- Next heartbeat:
- Workers awaiting reports:
- Stall watch: <worker: last activity, six-minute inactivity deadline, two-minute grace deadline, command/dependency evidence>
- Wait cursors / per-target errors:

Worker reports:
- <worker>: pending/received, commit <sha>, report <worktree-path>/.threadweave/report.md

Merge order:
1. <worker branch>
2. <worker branch>

Integration log:
- <branch>: <pre-merge review result> / <merge result> / <per-merge verification result>

Final review:
- pending/complete/failed/aborted/cleanup_pending (<reason>)

Cleanup:
- Preserved report/notes destinations:
- Outstanding async operations / unmerged branches / ignored or untracked files:
- Per-resource cleanup evidence, failures and next action:
- Heartbeat automation deleted: yes/no/n-a
- Leader-created worktrees removed: yes/no/n-a
```
