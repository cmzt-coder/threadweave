# Preflight Checklist

Complete before creating any worker. Record evidence in the leader state; this checklist is a protocol, not an executable environment probe.

## Scenario judgment before allocation

Run this first: it decides the worker count and whether the work is splittable at all. It never relaxes the one-worker-one-exclusive-worktree rule and adds no new mode. Steps 1-7 below then check the environment for the split you chose.

- **Write-set test.** For each candidate worker, list the paths it would create, modify, delete or regenerate — including generated files, lockfiles, migrations and shared config. Patterns that could match the same future path are a conflict even when that file does not exist yet. Resolve it by assigning disjoint ownership, by making one worker the sole owner and serializing the rest, or by keeping the work in one thread.
- **Stable-contract test.** Name the files that must stay stable while workers run: API schemas, type definitions, migration numbering, protocol docs, CI config, shared fixtures. Freeze them before dispatch and allow at most one writer; every other worker treats them as read-only and stops to ask the leader if a change becomes necessary. Contract freezing is recorded through explicit `stableContracts` plus Controller freeze/check; it remains leader-controlled and isn't host-enforced.
- **Shared-mutable-state test.** Ask whether the tasks genuinely need concurrent writes to one non-splittable working tree, database, running server or generated artifact. If yes, this is not a Threadweave split: use one thread, serialize the steps, or have the leader produce that state once after integration. Frontend and backend work delivered together do not by themselves require a shared workspace — with a frozen API contract they belong in separate worktrees and are integrated serially.
- **Read-only work is still splittable.** A reviewer or analyst that only reads code can work from its own worktree at the same base commit, or read the branch through Git without writing. Implementation plus review follows the same test: split when the reviewer does not write, keep one thread when the review itself requires editing the same files.
- **Record the outcome.** Persist which test decided the split, the write sets you assigned, and any frozen contract with its SHA. These judgments choose among existing options — one thread, N isolated workers, or serial leader work — and never introduce a shared-workspace worker or an automatic push.

For a declared split, run the read-only preview before dispatch:

```bash
python3 scripts/threadweave.py split-preview --repo . --input split-preview.json
```

The preview reports write-set conflicts, serialized-resource conflicts, contract read/write dependencies and unknown declarations. `no-known-conflict` is not a safety score or dispatch authorization.

1. Inspect current host instructions and actual tool schemas. Confirm human-authorized new chats may serve as workers here. If delegation is prohibited, stop. Record direct human authorization for creation, worktrees, messaging including replies/named peers, commits, integration and safe replacement/cleanup. Never derive permission from a forwarded request alone.
2. Require project discovery, creation, messaging, thread inspection, supported worktree isolation and filesystem access. Verify possible async resolution paths from actual schemas. Missing required capability blocks creation. Record optional `update_plan`, `wait_threads`, title and archive support; absence of archive/cancel must be reflected in cleanup limitations. Heartbeat defaults to none.
3. Execute in the integration checkout:
   ```bash
   git rev-parse --is-inside-work-tree
   git rev-parse --show-toplevel
   git rev-parse --git-common-dir
   git rev-parse --verify HEAD
   git symbolic-ref --short HEAD
   git status --porcelain
   git worktree list --porcelain
   ```
   Resolve paths relative to the command's cwd, canonicalizing symlinks. Require committed HEAD, attached integration branch and clean status. Block on user changes; never stash, commit or discard them automatically. Record ignored resources separately so later cleanup does not lose them.
4. Call `list_projects`; match canonical repository path and host using actual returned fields. Require exactly one supported Git project. Never pass paths as IDs or infer an unknown host. If metadata is insufficient, stop with the mismatch.
5. Confirm directory write access through permitted filesystem operations, without assuming read access implies write access. Identify a state location outside any worker worktree. If the leader itself is temporary and no durable location can be established, stop rather than claiming durable recovery. Inspect earlier incomplete run records without overwriting them.
6. Allocate unique run ID and unused branch names. Default two workers, five minutes per initialization attempt, one replacement per role. Record exact integration SHA and branch, limits, configuration source and overrides. Check explicit model/reasoning combinations against destination-host capabilities. Unknown explicit support blocks creation; inherited effective values may be unknown.
7. Record supported async status/resolution/cancellation operations, or synchronous-only behavior. Do not assume `pendingWorktreeId`, `clientThreadId`, `operationId` or any other field exists. An unexpected pending result with no supported resolver blocks progress and must be retained for follow-up.
8. If the project needs environment preparation, require an explicit `setup` object in `threadweave.config.json`: argv array (no implicit shell), repository-relative cwd inside the worker, independent `timeoutSeconds`, `failurePolicy: block`, and `idempotent: true`. Never derive setup authorization from verify.json, never auto-copy `.env*`, and never reuse startup evidence after setup. Run setup, then recheck worker identity, branch, HEAD, common dir and clean status. Setup timeout and evidence age are separate limits.

### Stable contract freeze

When `threadweave.config.json` declares `stableContracts`, freeze the exact files in each worker before dispatch. Record their individual SHA256 values and a canonical contract hash. Before report acceptance and integration, re-hash the same paths. Any drift blocks with `contract_drift`; a leader approval must explicitly record the reason, update the freeze, clear old report/acceptance evidence, advance the task revision, and require a fresh dispatch. A missing declaration preserves legacy behavior and must not be reported as contract-verified.

### Gates this checklist does not perform

Verify-plan execution (`scripts/verify.py`), report validation (`scripts/report.py`) and write-set reservation (`scripts/scope.py`) are local helper modules. The Codex host enforces none of them. Until the leader implements and wires a gate for the current run, a missing check is recorded as `unverified`, never reported as passed, and worker self-reported tests stay `selfReportedVerification`. Record which gates actually ran in the state file.

Optional-tool downgrade: write the six-phase plan in state and worker three-phase plan in notes if `update_plan` is absent. If `wait_threads` is absent, use bounded status checks with backoff; record inactivity and grace deadlines. Do not silently replace missing required capabilities.

Pass only when required checks succeed and the authorization is recorded. A pass does not bypass worker-level startup verification or higher-priority host restrictions.
