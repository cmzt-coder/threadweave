"""生命周期桥接的本地故障回归；所有宿主返回均为 fixture。"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller
from desktop_bridge import BridgeJournal, BridgePending, digest, now
from desktop_lifecycle_bridge import (DesktopLifecycleBridgeAdapter, WAIT, CREATE_WORKTREE,
                                     ATTACH_WORKTREE, WORKTREE_STATUS, ARCHIVE_WORKTREE,
                                     ARCHIVE_THREAD, complete_tool_result)
from host_adapter import UnsupportedHostAdapter
from state_store import read_state, run_state_path

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def rejects(name, fn, error=Exception):
    try:
        fn()
    except error:
        checks.append(name)
    else:
        raise AssertionError(name)


def raw(result, failed=False):
    return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': failed}


def poll(status='idle', turn='completed', final=True):
    return {'cursor': 'fixture:1', 'thread': {'id': 't-a', 'hostId': 'local', 'status': {'type': status}},
            'latestTurn': {'id': 'turn-a', 'status': turn},
            'latestAssistantMessage': {'turnId': 'turn-a', 'phase': 'final_answer' if final else 'commentary', 'text': 'fixture done ' + 'b'*64}}


def state():
    value = json.loads((project / 'references/state.example.json').read_text())
    value.update(runId='lifecycle-fixture', status='observing', revision=0)
    value['workers'] = [value['workers'][0]]
    w = value['workers'][0]
    w.update(threadId='t-a', state='reported', activityRevision=3,
             report={'threadId': 't-a', 'branch': w['branch'], 'head': w['baseSha'],
                     'reportSha': 'b'*64, 'taskRevision': 3, 'baseIsAncestor': True, 'acceptedAt': now()})
    return value


with tempfile.TemporaryDirectory() as td:
    def adapter(op, **kwargs):
        return DesktopLifecycleBridgeAdapter(td, 'lifecycle-fixture', op, 'a', 1, 3, **kwargs)
    a = adapter('wait-1', wait_cursors={'t-a': 'fixture:0'})
    c = Controller(a, td, 'lifecycle-fixture', state())
    c.save()
    path = run_state_path(td, 'lifecycle-fixture')
    before = path.read_bytes()
    rejects('controller-wait-queues', lambda: c.wait_for_workers(['t-a'], 0), BridgePending)
    r = a.journal.inspect('wait-1')
    check('cursor-and-zero-timeout-persisted', r['request']['params'] == {
        'targets': [{'threadId': 't-a', 'hostId': 'local', 'afterCursor': 'fixture:0'}], 'timeoutMs': 0})
    check('pending-wait-does-not-change-state', path.read_bytes() == before)
    for name, ids, seconds in [('duplicate', ['t-a', 't-a'], 0), ('empty', [], 0),
                               ('client-id', ['client-new-thread:x'], 0), ('over-budget', ['t-a'], 61),
                               ('bool-budget', ['t-a'], True)]:
        rejects('invalid-wait-' + name, lambda ids=ids, seconds=seconds: adapter('bad-' + name).wait_threads(ids, seconds))
    a.journal.claim('wait-1')
    bad = poll(); bad['thread']['id'] = 'other'
    rejects('cross-thread-poll-rejected', lambda: complete_tool_result(a.journal, 'wait-1', raw({'timedOut': False, 'polls': [bad]})))
    a.journal.mark_unknown('wait-1', 'fixture lost result')
    timed = raw({'timedOut': True, 'wake': None, 'polls': [poll('active', 'inProgress', False)]})
    rejects('unknown-wait-needs-reconciliation', lambda: complete_tool_result(a.journal, 'wait-1', timed))
    complete_tool_result(a.journal, 'wait-1', timed, 'fixture original result recovered')
    saved = a.journal._path('wait-1').read_bytes()
    observed = a.wait_threads(['t-a'], 0)
    rejects('timeout-active-does-not-confirm-stop', lambda: c.confirm_stop_from_wait('a', 1, observed))
    check('timeout-does-not-mutate-state', path.read_bytes() == before)
    check('timeout-retains-cursor', observed['metadata']['result']['polls'][0]['cursor'] == 'fixture:1')
    rejects('wait-cursor-rebinding-rejected', lambda: adapter('wait-1').wait_threads(['t-a'], 0))
    check('cursor-rejection-does-not-rewrite', a.journal._path('wait-1').read_bytes() == saved)
    rejects('completed-wait-not-reclaimed', lambda: a.journal.claim('wait-1'))

    done = adapter('wait-2')
    rejects('new-observation-needs-new-operation', lambda: done.wait_threads(['t-a'], 0), BridgePending)
    done.journal.claim('wait-2')
    complete_tool_result(done.journal, 'wait-2', raw({'timedOut': False, 'wake': None, 'polls': [poll()]}))
    observation = done.wait_threads(['t-a'], 0)
    for label, altered in [('old-revision', dict(observation, taskRevision=2)),
                            ('future', dict(observation, observedAt='2099-01-01T00:00:00Z')),
                            ('stale', dict(observation, observedAt=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()))]:
        rejects('stop-rejects-' + label, lambda altered=altered: c.confirm_stop_from_wait('a', 1, altered))
    missing_final = copy.deepcopy(observation)
    missing_final['metadata']['result']['polls'][0]['latestAssistantMessage']['phase'] = 'commentary'
    rejects('commentary-is-not-final', lambda: c.confirm_stop_from_wait('a', 1, missing_final))
    previous_turn = copy.deepcopy(observation)
    previous_turn['metadata']['result']['polls'][0]['latestAssistantMessage']['text'] = 'previous revision ' + 'c'*64
    rejects('old-final-cannot-stop-new-task', lambda: c.confirm_stop_from_wait('a', 1, previous_turn))
    with patch.object(c, 'save', side_effect=OSError('fixture save failure')):
        snapshot = copy.deepcopy(c.state)
        rejects('stop-save-failure-raised', lambda: c.confirm_stop_from_wait('a', 1, observation), OSError)
        check('stop-save-failure-memory-rollback', c.state == snapshot)
    check('stop-save-failure-disk-unchanged', path.read_bytes() == before)
    check('final-idle-confirms-stop', c.confirm_stop_from_wait('a', 1, observation) == 'confirmed')
    check('stop-bound-to-task-revision', c.state['workers'][0]['stopActivityRevision'] == 3)

    archiver = adapter('archive-a', archive_threads=['t-a'])
    c.adapter = archiver
    rejects('cannot-archive-running-worker', lambda: c.archive_worker_host('a', 1))
    check('rejected-archive-has-no-operation', not archiver.journal._path('archive-a').exists())
    # 模拟已完成独立集成的领域 fixture；本测试不声称执行真实 merge。
    c.state['workers'][0]['state'] = 'integrated'; c.save()
    rejects('integrated-archive-queues', lambda: c.archive_worker_host('a', 1), BridgePending)
    check('pending-archive-stays-integrated', read_state(path)['workers'][0]['state'] == 'integrated')
    archiver.journal.claim('archive-a')
    rejects('wrong-archive-id-rejected', lambda: complete_tool_result(archiver.journal, 'archive-a', raw({'threadId': 'wrong', 'archived': True})))
    complete_tool_result(archiver.journal, 'archive-a', raw({'threadId': 't-a', 'archived': True}))
    archive_bytes = archiver.journal._path('archive-a').read_bytes(); durable = path.read_bytes()
    with patch.object(c, 'save', side_effect=OSError('fixture archive save failure')):
        snapshot = copy.deepcopy(c.state)
        rejects('archive-save-failure-raised', lambda: c.archive_worker_host('a', 1), OSError)
        check('archive-save-failure-rolls-back-memory', c.state == snapshot)
    check('archive-save-failure-preserves-disk', path.read_bytes() == durable)
    restarted = Controller(adapter('archive-a', archive_threads=['t-a']), td, 'lifecycle-fixture', read_state(path))
    restarted.archive_worker_host('a', 1)
    check('archive-resumes-from-same-receipt', read_state(path)['workers'][0]['state'] == 'archived')
    check('archive-recovery-does-not-rewrite-receipt', archiver.journal._path('archive-a').read_bytes() == archive_bytes)
    complete_tool_result(archiver.journal, 'archive-a', raw({'threadId': 't-a', 'archived': True}))
    check('same-raw-completion-idempotent', archiver.journal._path('archive-a').read_bytes() == archive_bytes)
    durable = path.read_bytes(); restarted.archive_worker_host('a', 1)
    check('archive-replay-does-not-save-again', path.read_bytes() == durable)

    for label, call in [('create-disabled', lambda: adapter('no-create').create_worktree('fixture')),
                        ('attach-disabled', lambda: adapter('no-attach').attach_worktree('/fixture')),
                        ('archive-root-disabled', lambda: adapter('no-root').archive_worktree('/fixture')),
                        ('archive-thread-disabled', lambda: adapter('no-thread').archive_thread('t-a')),
                        ('default-attach', lambda: UnsupportedHostAdapter().attach_worktree('/fixture')),
                        ('default-archive-thread', lambda: UnsupportedHostAdapter().archive_thread('t-a'))]:
        rejects(label, call)
    config = {'sourceRoot': '/fixture/repo', 'ref': 'fixture-base'}
    resource = adapter('wt-create', worktree_config=config)
    rejects('explicit-resource-create-queues', lambda: resource.create_worktree('fixture-wt'), BridgePending)
    claim = resource.journal.claim('wt-create')
    resource.journal.mark_unknown('wt-create', 'fixture lost creation response')
    rejects('unknown-create-never-reclaims', lambda: resource.journal.claim('wt-create'))
    complete_tool_result(resource.journal, 'wt-create', raw({'operationId': 'fixture-host-op', 'status': 'creating'}), 'fixture original create response')
    result = resource.create_worktree('fixture-wt')
    check('async-operation-not-a-directory', result['metadata']['result'] == {'operationId': 'fixture-host-op', 'status': 'creating'})
    rejects('changed-source-ref-not-recreated', lambda: adapter('wt-create', worktree_config=dict(config, ref='other')).create_worktree('fixture-wt'))
    operations = [('wt-status', lambda a: a.worktree_creation_status('fixture-host-op'), {'status': 'completed', 'workspaceDirectory': '/fixture/wt'}),
                  ('wt-attach', lambda a: a.attach_worktree('/fixture/wt'), {'workspaceDirectory': '/fixture/wt'}),
                  ('wt-archive', lambda a: a.archive_worktree('/fixture/wt'), {'status': 'queued'})]
    for op, call, response in operations:
        item = adapter(op, attach_paths=['/fixture/wt'], archive_roots=['/fixture/wt'])
        rejects(op + '-queues', lambda: call(item), BridgePending)
        item.journal.claim(op); complete_tool_result(item.journal, op, raw(response))
        check(op + '-raw-result-retained', call(item)['metadata']['result'] == response)
        check(op + '-request-bound-to-revision', item.journal.inspect(op)['request']['binding']['taskRevision'] == 3)
    failure = adapter('archive-primary', archive_roots=['/fixture/primary'])
    rejects('primary-request-queues', lambda: failure.archive_worktree('/fixture/primary'), BridgePending)
    failure.journal.claim('archive-primary')
    complete_tool_result(failure.journal, 'archive-primary', raw('primary checkout cannot be removed', True))
    rejects('host-primary-rejection-propagated', lambda: failure.archive_worktree('/fixture/primary'), RuntimeError)
    rejects('failed-primary-result-cannot-be-overwritten', lambda: complete_tool_result(failure.journal, 'archive-primary', raw({'status': 'queued'})))
    check('lifecycle-capabilities-accurate', adapter('cap').capabilities['wait_threads'] and not adapter('cap').capabilities['archive_worktree'])

    # 公开 CLI 新进程回读，不加载 Python 内存中的对象。
    cli = subprocess.run([sys.executable, str(project/'scripts/desktop_lifecycle_bridge.py'), 'inspect',
                          '--repo', td, '--run', 'lifecycle-fixture', '--operation', 'wt-archive'], capture_output=True, text=True)
    check('cli-fresh-process-reads-queued-archive', cli.returncode == 0 and json.loads(cli.stdout)['response']['metadata']['result']['status'] == 'queued')

print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False))
