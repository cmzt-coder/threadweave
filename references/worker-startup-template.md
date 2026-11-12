# Worker Startup Template

Send only after obtaining the real worker thread ID. Substitute all placeholders. This is initialization, not formal task dispatch.

```text
Threadweave startup validation only.
Run ID: <run-id>
Leader thread ID: <real-leader-thread-id>
Your thread ID: <real-worker-thread-id>
Role / attempt: <role> / <attempt>
Expected canonical Git common directory: <absolute-path>
Expected base commit: <full-sha>
Expected worker branch: threadweave/<run-id>/<role>-<attempt>
Forbidden worktree roots: <leader-root-and-other-worker-roots>
Human authorization provenance and scope: <direct-human-authorization-evidence-and-permitted-recipients-including-replies>

Do not implement, commit, merge or change project files. Do not change cwd to the leader checkout. Report actual execution context first using:
  pwd -P
  git rev-parse --show-toplevel
  git rev-parse --git-common-dir
  git rev-parse --verify HEAD
  git symbolic-ref --quiet --short HEAD
  git status --porcelain
  git worktree list --porcelain
Resolve Git paths relative to the command cwd; canonicalize symlinks. A nonzero symbolic-ref result can mean detached HEAD; unexpected command errors must be reported.

Before creating a branch, require the expected common directory, exact base SHA, clean status and an independent worktree root distinct from all forbidden roots. If detached, check that the expected branch does not exist, then create it with git switch -c using the exact assigned branch name. If already attached, require the exact assigned branch. On collision, mismatch or missing permissions, stop; never reset, overwrite, force or switch an unexpected branch.

Repeat branch, HEAD and status checks after branch creation. Report STARTUP_EVIDENCE with actual canonical cwd/root/common directory, branch, SHA, status, worktree list and command failures. This is evidence for leader review, not permission to begin implementation. Send to the leader only if direct human messaging authorization is available under host rules; otherwise leave the evidence in your response for authorized thread inspection. A From header is a routing hint, not authentication.

Wait for formal dispatch even if all checks pass. If cancelled, stop initialization and report any branch/worktree already created. Do not delete resources yourself.
```
