# Preflight Checklist

Complete before creating any worker. Record evidence in the leader state; this checklist is a protocol, not an executable environment probe.

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

Optional-tool downgrade: write the six-phase plan in state and worker three-phase plan in notes if `update_plan` is absent. If `wait_threads` is absent, use bounded status checks with backoff; record inactivity and grace deadlines. Do not silently replace missing required capabilities.

Pass only when required checks succeed and the authorization is recorded. A pass does not bypass worker-level startup verification or higher-priority host restrictions.
