"""桌面写桥接的本地契约测试；不创建真实线程、不发送宿主消息。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller, StaleStateError
from desktop_bridge import (BridgeJournal, BridgePending, CREATE_METHOD, SEND_METHOD,
                            DesktopWriteBridgeAdapter, digest, now)
from state_store import read_state, run_state_path

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def rejects(name, function, error=Exception):
    try:
        function()
    except error:
        checks.append(name)
    else:
        raise AssertionError(name)


def receipt(record, metadata=None, error=None):
    value = {k: record[k] for k in ('operationId', 'runId', 'requestSha', 'claimToken')}
    value.update(method=record['request']['method'], source='desktop_tool_bridge',
                 observedAt=now(), status='failed' if error else 'succeeded',
                 toolResultSha=digest({'fixture': True}))
    value['error' if error else 'metadata'] = error if error else metadata
    return value


config = {'model': 'deepseek-flash', 'thinking': 'high', 'title': 'fixture-create', 'branchName': 'fixture-base'}


def adapter(td, op, revision=0, cfg=config):
    return DesktopWriteBridgeAdapter(td, 'write-probe', op, 'probe', 1,
                                     task_revision=revision, create_config=cfg)


with tempfile.TemporaryDirectory() as td:
    a = adapter(td, 'create-1')
    j = a.journal
    rejects('create-first-queues', lambda: a.create_worker('startup only', 'project'), BridgePending)
    queued = j.inspect('create-1')
    check('model-reasoning-explicit', queued['request']['params']['model'] == 'deepseek-flash'
          and queued['request']['params']['thinking'] == 'high')
    check('existing-baseline-and-worktree', queued['request']['params']['target']['environment'] == {
        'type': 'worktree', 'startingState': {'type': 'branch', 'branchName': 'fixture-base'}})
    rejects('changed-prompt-not-recreated', lambda: a.create_worker('different', 'project'))
    rejects('changed-project-not-recreated', lambda: a.create_worker('startup only', 'other'))
    changed = adapter(td, 'create-1', cfg=dict(config, model='other'))
    rejects('changed-model-not-recreated', lambda: changed.create_worker('startup only', 'project'))
    claim = j.claim('create-1')
    j.mark_unknown('create-1', 'lost response')
    rejects('unknown-create-no-new-call', lambda: a.create_worker('startup only', 'project'), BridgePending)
    response = receipt(claim, {'clientThreadId': 'client-id', 'hostId': 'local'})
    rejects('unknown-create-needs-proof', lambda: j.complete('create-1', response))
    j.complete('create-1', response, 'recovered original create response')
    rejects('client-id-never-returned-as-thread', lambda: a.create_worker('startup only', 'project'), BridgePending)
    resolution = dict(clientThreadId='client-id', threadId='real-id', hostId='local',
                      projectId='project', title=config['title'], model=config['model'],
                      thinking=config['thinking'], branchName=config['branchName'], cwd='/fixture/worktree',
                      observedAt=now(), toolResultSha=digest({'fixture': 'metadata'}),
                      evidence='fixture exact client linkage + independent branch/model/project checks')
    for key in ('clientThreadId', 'projectId', 'title', 'model', 'thinking', 'branchName', 'hostId'):
        bad = dict(resolution, **{key: 'wrong'})
        rejects('resolution-wrong-' + key, lambda bad=bad: j.resolve_creation('create-1', bad))
    rejects('client-is-not-real-id', lambda: j.resolve_creation('create-1', dict(resolution, threadId='client-id')))
    rejects('resolution-needs-evidence', lambda: j.resolve_creation('create-1', dict(resolution, evidence='')))
    rejects('resolution-future-rejected', lambda: j.resolve_creation('create-1', dict(resolution, observedAt='2099-01-01T00:00:00Z')))
    j.resolve_creation('create-1', resolution)
    before = j._path('create-1').read_bytes()
    j.resolve_creation('create-1', resolution)
    check('resolution-replay-no-write', j._path('create-1').read_bytes() == before)
    rejects('resolved-id-immutable', lambda: j.resolve_creation('create-1', dict(resolution, threadId='other-id')))
    check('restart-returns-actual-id', adapter(td, 'create-1').create_worker('startup only', 'project') == {
        'threadId': 'real-id', 'hostId': 'local'})
    check('create-replay-preserves-original-bytes', j._path('create-1').read_bytes() == before)

    direct = adapter(td, 'direct-create')
    rejects('direct-first-queues', lambda: direct.create_worker('startup', 'project'), BridgePending)
    direct_claim = j.claim('direct-create')
    bad = receipt(direct_claim, {'threadId': 'real', 'clientThreadId': 'client', 'hostId': 'local'})
    rejects('ambiguous-create-result-rejected', lambda: j.complete('direct-create', bad))
    j.complete('direct-create', receipt(direct_claim, {'threadId': 'direct-id', 'hostId': 'local'}))
    check('direct-create-returned', direct.create_worker('startup', 'project')['threadId'] == 'direct-id')
    disabled = adapter(td, 'disabled', cfg=None)
    rejects('create-requires-config', lambda: disabled.create_worker('startup', 'project'), NotImplementedError)
    params = copy.deepcopy(queued['request']['params'])
    params['target']['environment'] = {'type': 'local'}
    rejects('local-worktree-not-silently-used', lambda: j.prepare_create('bad-local', 'probe', 1, 0, params))
    params = copy.deepcopy(queued['request']['params'])
    params['target']['environment']['startingState']['onMissing'] = 'create-branch'
    rejects('baseline-not-auto-created', lambda: j.prepare_create('bad-base', 'probe', 1, 0, params))
    params = {'threadId': 'real-id', 'hostId': 'local', 'prompt': 'message', 'model': 'other'}
    rejects('send-no-silent-model-switch', lambda: j._prepare('bad-send', 'probe', 1, 0, SEND_METHOD, params))

    send = adapter(td, 'send-1')
    rejects('send-first-queues', lambda: send.send_message('real-id', 'nonce request'), BridgePending)
    sc = j.claim('send-1')
    rejects('message-cannot-be-retargeted', lambda: send.send_message('other', 'nonce request'))
    rejects('message-cannot-be-edited', lambda: send.send_message('real-id', 'different'))
    rejects('send-receipt-not-completion', lambda: j.complete('send-1', receipt(sc, {
        'threadId': 'real-id', 'hostId': 'local', 'status': 'complete'})))
    sr = receipt(sc, {'threadId': 'real-id', 'hostId': 'local', 'status': 'accepted'})
    j.mark_unknown('send-1', 'lost receipt')
    rejects('unknown-message-no-resend', lambda: j.claim('send-1'))
    rejects('unknown-message-needs-proof', lambda: j.complete('send-1', sr))
    j.complete('send-1', sr, 'recovered matching original receipt')
    sb = j._path('send-1').read_bytes()
    check('send-restart-reads-receipt', adapter(td, 'send-1').send_message('real-id', 'nonce request')['status'] == 'accepted')
    check('send-replay-no-write', j._path('send-1').read_bytes() == sb)
    rejects('resume-cannot-prepare-new-operation', lambda: adapter(td, 'absent').completed_message('real-id', 'message'))
    check('resume-absent-stays-absent', not j._path('absent').exists())

    state = json.loads((project / 'references/state.example.json').read_text())
    state.update(runId='write-probe', status='dispatching', revision=0)
    state.update(resources=[], mergeOrder=[], integrationLog=[])
    state['workers'] = [dict(state['workers'][0], role='probe', threadId='real-id',
                             state='ready', activityRevision=0, worktree='/fixture/worktree')]
    creating = copy.deepcopy(state)
    creating['workers'][0].update(state='pending', threadId=None)
    cc = Controller(adapter(td, 'controller-create'), td, 'write-probe', creating)
    project_id = creating['project']['projectId']
    rejects('controller-create-project-binding', lambda: cc.create_worker('startup', 'other'))
    check('rejected-project-no-request', not j._path('controller-create').exists())
    rejects('controller-create-queues', lambda: cc.create_worker('startup', project_id), BridgePending)
    other_run = copy.deepcopy(creating); other_run['runId'] = 'other-run'
    rejects('controller-create-run-binding', lambda: Controller(adapter(td, 'wrong-run'), td, 'write-probe', other_run).create_worker('startup', project_id))
    check('rejected-run-no-request', not j._path('wrong-run').exists())
    rejects('controller-no-create-over-registered-worker', lambda: Controller(adapter(td, 'registered'), td, 'write-probe', copy.deepcopy(state)).create_worker('startup', project_id))
    noauth = copy.deepcopy(creating); noauth['authorization'] = {}
    rejects('controller-create-needs-authorization', lambda: Controller(adapter(td, 'noauth'), td, 'write-probe', noauth).create_worker('startup', project_id))
    worker = state['workers'][0]
    report = dict(role='probe', attempt=1, observedAt=now(), cwd=worker['worktree'],
                  worktree_root=worker['worktree'], git_common_dir=state['project']['commonDirectory'],
                  branch=worker['branch'], head=worker['baseSha'], status_porcelain='')
    expected = dict(common_dir=state['project']['commonDirectory'], other_roots=[],
                    integration_head=state['integration']['baseSha'])
    wrong_binding = Controller(adapter(td, 'bad-dispatch', revision=1), td, 'write-probe', copy.deepcopy(state))
    before_wrong = copy.deepcopy(wrong_binding.state)
    rejects('dispatch-binding-checked-before-intent', lambda: wrong_binding.dispatch('probe', 1, 'formal nonce', report, expected))
    check('invalid-binding-leaves-memory-intact', wrong_binding.state == before_wrong)
    c = Controller(adapter(td, 'dispatch-1'), td, 'write-probe', state)
    c.save()
    rejects('controller-queue-remains-pending', lambda: c.dispatch('probe', 1, 'formal nonce', report, expected), BridgePending)
    saved = read_state(run_state_path(td, 'write-probe'))
    check('intent-not-unknown-on-queue', saved['workers'][0]['dispatchIntent']['result'] == 'pending')
    check('queued-worker-not-dispatched', saved['workers'][0]['state'] == 'ready')
    check('startup-and-op-persist-before-send', saved['workers'][0]['dispatchIntent']['bridgeOperationId'] == 'dispatch-1'
          and 'startupEvidence' in saved['workers'][0])
    rejects('duplicate-dispatch-blocked', lambda: c.dispatch('probe', 1, 'formal nonce', report, expected))
    rejects('resume-awaits-receipt', lambda: c.resume_bridge_dispatch('probe', 1), BridgePending)
    dc = j.claim('dispatch-1')
    j.complete('dispatch-1', receipt(dc, {'threadId': 'real-id', 'hostId': 'local', 'status': 'accepted'}))
    wrong = Controller(adapter(td, 'dispatch-1', revision=1), td, 'write-probe', copy.deepcopy(saved))
    rejects('receipt-cannot-cross-task-revision', lambda: wrong.resume_bridge_dispatch('probe', 1))
    stopped = copy.deepcopy(saved); stopped['workers'][0]['stopStatus'] = 'requested'
    rejects('stopping-worker-cannot-resume', lambda: Controller(adapter(td, 'dispatch-1'), td, 'write-probe', stopped).resume_bridge_dispatch('probe', 1))
    terminal = copy.deepcopy(saved); terminal['status'] = 'complete'
    rejects('terminal-run-cannot-resume', lambda: Controller(adapter(td, 'dispatch-1'), td, 'write-probe', terminal).resume_bridge_dispatch('probe', 1))
    restarted = Controller(adapter(td, 'dispatch-1'), td, 'write-probe', copy.deepcopy(saved))
    with patch.object(restarted.adapter, 'send_message', side_effect=AssertionError('must not resend')):
        check('resume-advances-without-send', restarted.resume_bridge_dispatch('probe', 1) == 'real-id')
    durable = read_state(run_state_path(td, 'write-probe'))
    check('receipt-advances-state-once', durable['workers'][0]['state'] == 'dispatched'
          and durable['workers'][0]['activityRevision'] == 1)
    check('intent-sent-after-receipt', durable['workers'][0]['dispatchIntent']['result'] == 'sent')
    rejects('second-resume-blocked', lambda: restarted.resume_bridge_dispatch('probe', 1))
    stale = Controller(adapter(td, 'dispatch-1'), td, 'write-probe', copy.deepcopy(saved))
    snapshot = copy.deepcopy(stale.state)
    rejects('stale-resume-cannot-overwrite', lambda: stale.resume_bridge_dispatch('probe', 1), StaleStateError)
    check('stale-resume-restores-memory', stale.state == snapshot)
    check('stale-resume-keeps-durable-dispatch', read_state(run_state_path(td, 'write-probe')) == durable)

print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False))
