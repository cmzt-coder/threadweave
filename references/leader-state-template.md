# Leader State Template

Use this compact record while coordinating a thread team.

Persist it to `.thread-team/<leader-thread-id>/state.md` under the repo root (one directory per run, keyed by leader thread ID), kept out of commits via the exclude file located by `git rev-parse --git-path info/exclude` (in a linked worktree `.git` is a file, so do not hardcode `.git/info/exclude`). Update it immediately after every state change. After compaction or interruption, re-derive the path from your own leader thread ID and reread this file before acting.

```md
Thread-team leader state.

Leader thread ID:
Integration branch:
Repo path:
User objective:
Worker runtime settings (as confirmed by the user; defaults: gpt-5.4 / high):
- Model: <confirmed-model>
- Thinking: <confirmed-thinking>

Team roster:
| Role | Thread ID | Branch | Working directory | Responsibility | Startup | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Leader | <id> | <branch> | <leader-checkout-path> | Plan, decide, poll, merge, final review | n/a | active |
| <worker> | <id> | <branch> | <worktree-path> | <scope> | not-started/started/replaced | registered/dispatched/running/blocked/reported/integrated/archived |

Retired workers:
- <old worker>: archived, replaced by <new worker>, reason <startup stuck/no post-dispatch response/etc.>

Dependency map:
- <dependency>

Decision log:
- <decision>

Polling state:
- Phase 4 mode: startup-gate/heartbeat-polling/complete
- Heartbeat automation: <id, or none / deleted>
- Last heartbeat:
- Next heartbeat:
- Workers awaiting reports:
- Stall watch: <worker: passes with no activity, or none>

Worker reports:
- <worker>: pending/received, commit <sha>, report <worktree-path>/.thread-team/report.md

Merge order:
1. <worker branch>
2. <worker branch>

Integration log:
- <branch>: <pre-merge review result> / <merge result> / <per-merge verification result>

Final review:
- pending/complete/aborted (<reason>)

Cleanup:
- Heartbeat automation deleted: yes/no/n-a
- Leader-created worktrees removed: yes/no/n-a
```
