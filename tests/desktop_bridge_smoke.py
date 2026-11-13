"""验证桌面桥接关联、未决恢复和控制器接线；不调用真实宿主。"""
import copy
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller, StaleStateError
from desktop_bridge import BridgeJournal, BridgePending, DesktopBridgeAdapter, METHOD, digest, main, now
from state_schema import validate_state
from state_store import LockHeld, read_state, run_state_path, write_state_atomic
from state_view import write_state_view

checks = []


def check(name, value):
    assert value, name
    checks.append(name)


def rejects(name, function, error=Exception):
    try:
        function()
    except error:
        checks.append(name)
    else:
        raise AssertionError(name)


def response(record, status='succeeded'):
    result = {k: record[k] for k in ('operationId', 'runId', 'requestSha', 'claimToken')}
    result.update(method=METHOD, source='desktop_tool_bridge', observedAt=now(),
                  status=status, toolResultSha=digest({'fixture': True}))
    if status == 'succeeded':
        result['metadata'] = {'threadId': record['request']['params']['threadId'],
                              'hostId': 'local', 'status': 'idle'}
    else:
        result['error'] = 'fixture host error'
    return result


def sample_state(repo):
    state = json.loads((project / 'references/state.example.json').read_text())
    state.update(runId='probe-run', status='observing', revision=0)
    state['workers'] = [state['workers'][0]]
    state['workers'][0].update(role='probe', threadId='target', activityRevision=0)
    state['integrationLog'] = []
    return state


with tempfile.TemporaryDirectory() as td:
    journal = BridgeJournal(td, 'probe-run')
    adapter = DesktopBridgeAdapter(td, 'probe-run', 'read-1', 'probe', 1)
    rejects('first-read-is-pending', lambda: adapter.read_thread('target'), BridgePending)
    request = journal.inspect('read-1')
    check('request-precedes-execution', request['status'] == 'pending')
    check('read-only-tool-method', request['request']['method'] == METHOD)
    check('bounded-no-command-output', request['request']['params']['includeOutputs'] is False
          and request['request']['params']['turnLimit'] == 1)
    before = journal._path('read-1').read_bytes()
    rejects('reused-id-different-thread', lambda: journal.prepare_read('read-1', 'probe', 1, 0, 'other'))
    rejects('reused-id-different-attempt', lambda: journal.prepare_read('read-1', 'probe', 2, 0, 'target'))
    rejects('reused-id-different-task', lambda: journal.prepare_read('read-1', 'probe', 1, 1, 'target'))
    check('conflicts-leave-request-intact', journal._path('read-1').read_bytes() == before)
    claimed = journal.claim('read-1')
    rejects('cannot-claim-twice', lambda: journal.claim('read-1'), RuntimeError)
    restarted = DesktopBridgeAdapter(td, 'probe-run', 'read-1', 'probe', 1)
    rejects('restart-in-flight-remains-pending', lambda: restarted.read_thread('target'), BridgePending)
    check('restart-does-not-reset-in-flight', journal.inspect('read-1')['status'] == 'in_flight')
    valid = response(claimed)
    for field in ('operationId', 'runId', 'requestSha', 'claimToken', 'method', 'source'):
        bad = copy.deepcopy(valid); bad[field] = 'wrong'
        rejects('reject-wrong-' + field, lambda bad=bad: journal.complete('read-1', bad))
    bad = copy.deepcopy(valid); bad['metadata']['threadId'] = 'other'
    rejects('reject-wrong-result-thread', lambda: journal.complete('read-1', bad))
    bad = copy.deepcopy(valid); bad['observedAt'] = '2099-01-01T00:00:00Z'
    rejects('reject-future-result', lambda: journal.complete('read-1', bad))
    bad = copy.deepcopy(valid); bad['metadata']['transcript'] = 'do not retain'
    rejects('metadata-is-minimized', lambda: journal.complete('read-1', bad))
    journal.mark_unknown('read-1', 'leader interrupted before result was saved')
    rejects('unknown-not-resendable', lambda: journal.claim('read-1'))
    rejects('unknown-needs-reconciliation', lambda: journal.complete('read-1', valid))
    check('reconcile-preserves-claim', journal.inspect('read-1')['claimToken'] == claimed['claimToken'])
    done = journal.complete('read-1', valid, 'recovered matching original tool response')
    check('explicit-reconcile-completes', done['status'] == 'succeeded')
    before = journal._path('read-1').read_bytes()
    journal.complete('read-1', valid)
    check('duplicate-result-no-write', journal._path('read-1').read_bytes() == before)
    different = copy.deepcopy(valid); different['metadata']['status'] = 'active'
    rejects('terminal-result-immutable', lambda: journal.complete('read-1', different))
    observation = restarted.read_thread('target')
    check('read-after-restart-returns-saved-result', observation['metadata']['status'] == 'idle')
    check('result-bound-to-task-and-operation', observation['taskRevision'] == 0
          and observation['operationId'] == 'read-1' and observation['runId'] == 'probe-run')
    check('write-capabilities-disabled', not adapter.capabilities['create_worker']
          and not adapter.capabilities['send_message'])
    for name, args in [('create_worker', ('task', 'project')), ('send_message', ('target', 'task')),
                       ('create_worktree', ('worker',)), ('archive_worktree', ('root',)),
                       ('wait_threads', (['target'], 1))]:
        rejects('unsupported-' + name, lambda name=name, args=args: getattr(adapter, name)(*args), NotImplementedError)

    adapter2 = DesktopBridgeAdapter(td, 'probe-run', 'fail-1', 'probe', 1)
    rejects('failed-request-first-pending', lambda: adapter2.read_thread('target'), BridgePending)
    failure = response(journal.claim('fail-1'), 'failed')
    journal.complete('fail-1', failure)
    rejects('host-error-not-success-or-retry', lambda: adapter2.read_thread('target'), RuntimeError)
    check('failed-result-durable', journal.inspect('fail-1')['status'] == 'failed')

    state = sample_state(td)
    controller = Controller(adapter, td, 'probe-run', state)
    controller.save()
    state_before = copy.deepcopy(state)
    controller.observe_worker_host('probe', 1)
    stored = read_state(run_state_path(td, 'probe-run'))
    check('controller-observation-durable', stored['workers'][0]['hostObservation'] == observation)
    check('query-does-not-advance-lifecycle', stored['status'] == state_before['status']
          and stored['workers'][0]['state'] == state_before['workers'][0]['state'])
    check('query-does-not-confirm-stop-or-acceptance', 'stopStatus' not in stored['workers'][0]
          and 'acceptance' not in stored['workers'][0])
    check('query-is-not-worker-progress', stored['workers'][0]['lastObservation']
          == state_before['workers'][0]['lastObservation'])
    revision = state['revision']
    controller.observe_worker_host('probe', 1)
    check('replay-does-not-increment-state-revision', state['revision'] == revision)
    path = write_state_view(td, 'probe-run')
    view = path.read_text()
    check('view-derived-from-durable-observation', 'target' in view and 'read-1' in view)
    check('view-states-historical-limit', '历史观察' in view)
    path.write_text('manual fake success')
    write_state_view(td, 'probe-run')
    check('markdown-not-authoritative', path.read_text() == view)
    bad_state = copy.deepcopy(stored);bad_state['workers'][0]['hostObservation']['role'] = 'other'
    rejects('schema-rejects-observation-identity', lambda: validate_state(bad_state))
    wrong_adapter = DesktopBridgeAdapter(td, 'probe-run', 'read-1', 'other', 1)
    wrong_controller = Controller(wrong_adapter, td, 'probe-run', copy.deepcopy(stored))
    rejects('controller-rejects-cross-role', lambda: wrong_controller.observe_worker_host('probe', 1))
    stale = copy.deepcopy(stored)
    stale['workers'][0].pop('hostObservation')
    stale['revision'] -= 1
    stale_before = copy.deepcopy(stale)
    stale_controller = Controller(adapter, td, 'probe-run', stale)
    rejects('stale-controller-cannot-overwrite', lambda: stale_controller.observe_worker_host('probe', 1), StaleStateError)
    check('failed-save-rolls-back-memory', stale_controller.state == stale_before)

    for op in ('../escape', 'bad/name', '.', ''):
        rejects('reject-path-' + repr(op), lambda op=op: journal.prepare_read(op, 'probe', 1, 0, 'target'))
    journal.prepare_read('race', 'probe', 1, 0, 'target')
    def claimant(_):
        try:
            journal.claim('race')
            return True
        except (RuntimeError, LockHeld):
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        check('only-one-concurrent-claim', sum(pool.map(claimant, range(2))) == 1)
    tampered = journal.inspect('race');tampered['request']['params']['includeOutputs'] = True
    write_state_atomic(journal._path('race'), tampered)
    rejects('tampered-request-rejected', lambda: journal.inspect('race'))

    # CLI 通过控制器准备请求，明确返回 pending 而不是误报成功。
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            main(['observe', '--repo', td, '--run', 'probe-run', '--operation', 'cli-read', '--role', 'probe'])
        except SystemExit as exc:
            check('cli-pending-exit-three', exc.code == 3)
        else:
            raise AssertionError('CLI incorrectly returned success')
    check('cli-pending-record-created', journal.inspect('cli-read')['status'] == 'pending')
    mismatched = copy.deepcopy(stored)
    mismatched['runId'] = 'other-run'
    mismatch_adapter = DesktopBridgeAdapter(td, 'probe-run', 'wrong-run-read', 'probe', 1)
    mismatch_controller = Controller(mismatch_adapter, td, 'probe-run', mismatched)
    rejects('wrong-run-rejected-before-request', lambda: mismatch_controller.observe_worker_host('probe', 1))
    check('wrong-run-does-not-create-request', not journal._path('wrong-run-read').exists())

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / '.threadweave').symlink_to(root / 'outside')
    rejects('reject-symlink-state-root', lambda: BridgeJournal(root, 'run'))

print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False))
