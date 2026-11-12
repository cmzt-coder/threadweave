# Worker Report Template

Workers write this to `.thread-team/report.md` in their worktree root after implementation, self-review, verification, and commit — then send the leader a short message carrying only the `From:` header, `COMPLETE`, the report file path, and the commit SHA. Never paste this report into a message; the leader reads the file directly.

```text
Worker complete and ready for leader integration.

Worker thread ID: <worker-thread-id>
Role: <role>
Branch: <worker-branch>
Commit SHA: <sha>

Task completed:
<summary>

Files changed:
<files>

Self-review:
- Bugs or risks found and fixed: <summary or none>
- Scope drift checked: <yes/no plus notes>
- Generated/cache/unrelated files checked: <yes/no plus notes>
- Shared contracts preserved or changed: <summary>

Peer coordination:
- Peer threads contacted: <thread IDs and outcomes, or none>
- Decisions needing leader awareness: <summary or none>

Verification:
<commands run and results, or why not run>

Unresolved issues:
<none or details>

Ready for leader integration: <yes/no>
```
