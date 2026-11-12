# Final Report Template

Use this after all selected worker branches are merged and the leader has completed the final review.

```text
Thread-team task complete.

Leader thread:
- Thread ID: <leader-thread-id>
- Integration branch: <branch>
- Final HEAD: <sha>

Team records:
| Role | Thread ID | Branch | Responsibility | Result | Verification | Final status |
| --- | --- | --- | --- | --- | --- | --- |
| Leader | <id> | <branch> | Planning, decisions, serial integration, final review | <summary> | <checks> | complete |
| <worker> | <id> | <branch> | <scope> | <summary> | <checks> | integrated/excluded |

Merge order:
1. <worker branch> - <merge result> - <per-merge verification result>
2. <worker branch> - <merge result> - <per-merge verification result>

Final leader review:
- Objective satisfied: <yes/no>
- Bugs or integration defects found: <summary or none>
- Fixes made by leader after merge: <summary or none>
- Verification after final review: <commands/results>

Cleanup:
- Heartbeat automations deleted: <yes/n-a>
- Leader-created worker worktrees removed: <yes/n-a>

Remaining risks:
<none or details>
```
