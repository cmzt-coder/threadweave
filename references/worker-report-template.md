# Worker Report Template

Workers write this to `.threadweave/report.md` in their worktree root after implementation, self-review, verification, and commit — then send the leader a short message carrying only the `From:` header, `COMPLETE`, the report file path, and the commit SHA. Messaging requires direct human authorization valid under host rules. If unavailable, leave the completion signal in the worker response for leader inspection and record the limitation. Never paste this report into a message; the leader reads the file directly.

## Machine-readable sidecar

The Markdown below is explanatory. Machine gates read a JSON sidecar at `.threadweave/report.json` in the same worktree; a missing or malformed sidecar is a rejection, not a pass. `scripts/report.py` is the local validator, and the Codex host does not enforce it — the leader records whether validation actually ran for this run.

```json
{
  "runId": "<run-id>",
  "role": "<role>",
  "attempt": 1,
  "threadId": "<worker-thread-id>",
  "branch": "<worker-branch>",
  "head": "<full-40-character-sha>",
  "commitSha": "<must-equal-head-or-omit>",
  "reportSha": "<sha256-of-the-canonical-json-below>",
  "taskRevision": 0,
  "baseIsAncestor": true,
  "filesChanged": ["path/changed.py", "path/old_name.py", "path/new_name.py"],
  "testsRun": ["<verify-step-id>"],
  "selfReportedVerification": ["python3 tests/run_all.py"],
  "contractChanges": [],
  "knownRisks": []
}
```

Rules:

- `reportSha` covers the canonical JSON — sorted keys, no insignificant whitespace, UTF-8 — with `reportSha` and `acceptedAt` removed. It never covers this Markdown file, and a value computed over a payload that still contained `reportSha` is wrong.
- `commitSha`, when present, must equal `head`. Every list field (`filesChanged`, `testsRun`, `selfReportedVerification`, `contractChanges`, `knownRisks`) must be a list of strings; objects, `{id, status}` entries or a bare string where a list is required are rejections.
- `filesChanged` is compared against the leader's independently collected diff, ignoring order. List both sides of a rename and never list a file you did not change; the leader will not copy these values into its own observation.
- `testsRun` holds verify plan step IDs only — the IDs the leader's plan executed — never command lines and never result objects. Commands you ran yourself, and their outcomes, belong in `selfReportedVerification` and in the Markdown `Verification:` block.
- Every value here is self-reported. A perfectly matching report is only `accepted`; the leader's verification of `head`, `branch`, `baseIsAncestor` and `filesChanged` against Git is what makes it trusted.

```text
Worker complete and ready for leader integration.

Run ID: <run-id>
Working directory: <verified-worktree-root>
Base commit: <verified-full-base-sha>
Completion delivery: <authorized-message-or-response-for-inspection>
Worker thread ID: <worker-thread-id>
Role: <role>
Branch: <worker-branch>
Commit SHA: <sha>
Machine-readable sidecar: .threadweave/report.json

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
