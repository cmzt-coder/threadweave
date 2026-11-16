"""真实临时 Git 计划与宿主回执 fixture 的身份联检；不执行真实宿主归档。"""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cleanup_apply import preview
from managed_cleanup import execute_managed_cleanup

checks = []


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()


with tempfile.TemporaryDirectory() as td:
    base = Path(td).resolve()
    repo = base / 'repo'
    repo.mkdir()
    worktree = base / 'worker'
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Validation')
    git(repo, 'config', 'user.email', 'validation@example.invalid')
    (repo / 'tracked').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    git(repo, 'worktree', 'add', '-b', 'codex/cleanup-binding', str(worktree))
    report = base / 'report.md'
    report.write_text('verified')
    evidence_path = base / 'evidence.json'
    declaration = dict(
        runId='binding-test', repository=str(repo), worktree=str(worktree),
        branch='codex/cleanup-binding', head=git(worktree, 'rev-parse', 'HEAD'),
        integrationRef='main', report=str(report),
        reportSha256=hashlib.sha256(report.read_bytes()).hexdigest(), evidence=str(evidence_path))
    evidence = {k: declaration[k] for k in ('runId', 'worktree', 'branch', 'head', 'reportSha256')}
    evidence.update(
        observedAt=datetime.now(timezone.utc).isoformat(), ownedByRun=True,
        shared=False, acceptanceVerified=True, executionKind='host_thread',
        threadId='thread-a', stop=dict(threadIdle=True, finalAnswerPresent=True, noActiveTurn=True))

    def save(value):
        evidence_path.write_text(json.dumps(value))

    def receipts(thread_id):
        binding = dict(runId='binding-test', role='a', attempt=1, taskRevision=0,
                       source='desktop_tool_bridge', observedAt=datetime.now(timezone.utc).isoformat())
        stop = {**binding, 'operationId': 'wait', 'metadata': {'hostId': 'local', 'result': {
            'polls': [{'thread': {'id': thread_id, 'hostId': 'local', 'status': {'type': 'idle'}},
                       'latestTurn': {'id': 'turn', 'status': 'completed'},
                       'latestAssistantMessage': {'turnId': 'turn', 'phase': 'final_answer'}}]}}}
        inventory = {**binding, 'operationId': 'inventory', 'metadata': {'hostId': 'local', 'result': {
            'artifacts': [{'type': 'worktree', 'identityKey': str(worktree), 'payload': {
                'ownerThreadId': thread_id, 'isPrimary': False, 'isShared': False,
                'runId': 'binding-test'}}]}}}
        return stop, inventory

    adapter = Mock()

    def run(approved, stop, inventory, owner='thread-a'):
        adapter.reset_mock()
        return execute_managed_cleanup(
            adapter, declaration, approved, stop, inventory, owner, authorized=True)

    def blocked(name, approved, stop, inventory, owner='thread-a'):
        result = run(approved, stop, inventory, owner)
        assert result['status'] == 'blocked' and not adapter.archive_worktree.called, name
        assert worktree.exists() and report.exists() and evidence_path.exists(), name
        checks.append(name)

    save(evidence)
    approved = preview(declaration)
    stop, inventory = receipts('thread-b')
    blocked('cross-thread-stop-cannot-authorize', approved, stop, inventory, 'thread-b')

    stop, inventory = receipts('thread-a')
    result = run(approved, stop, inventory)
    assert result['status'] == 'host_result' and result['deletionPerformed'] is None
    adapter.archive_worktree.assert_called_once_with(str(worktree))
    checks.append('matching-thread-enters-fixture-adapter-only')

    local = {**evidence, 'executionKind': 'local_git', 'threadId': None,
             'stop': {'localProcessStopped': True}}
    save(local)
    blocked('local-process-evidence-cannot-authorize-host-archive', preview(declaration), stop, inventory)
    save(evidence)

    for name, value in [('null', None), ('array', []), ('string', 'invalid')]:
        bad = copy.deepcopy(stop)
        bad['metadata'] = value
        blocked('metadata-' + name, approved, bad, inventory)
        bad = copy.deepcopy(stop)
        bad['metadata']['result']['polls'][0]['thread']['status'] = value
        blocked('thread-status-' + name, approved, bad, inventory)
        bad = copy.deepcopy(inventory)
        bad['metadata']['result']['artifacts'][0]['payload'] = value
        blocked('ownership-payload-' + name, approved, stop, bad)

    bad = {**stop, 'observedAt': None}
    blocked('missing-observation-time', approved, bad, inventory)
    bad = copy.deepcopy(stop)
    bad['metadata']['result']['polls'] = [None]
    blocked('malformed-poll', approved, bad, inventory)
    bad = copy.deepcopy(inventory)
    bad['metadata']['result']['artifacts'] = [None]
    blocked('malformed-artifact', approved, stop, bad)
    # 旧批准计划不得在重新生成指纹后自动升级为新执行契约。
    legacy = copy.deepcopy(approved)
    legacy['plan'].pop('executionKind', None)
    legacy['plan'].pop('threadId', None)
    legacy['plan']['version'] = 1
    from cleanup import cleanup_plan_fingerprint
    legacy['fingerprint'] = cleanup_plan_fingerprint(legacy['plan'])
    blocked('legacy-plan-requires-new-preview', legacy, stop, inventory)

print(json.dumps({'passed': len(checks), 'checks': checks}))
