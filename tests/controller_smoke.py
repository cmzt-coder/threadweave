"""用 fake adapter 验证控制器纯逻辑编排，不代表真实宿主运行。"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller, StaleStateError  # noqa: E402
from host_adapter import HostAdapter, UnsupportedHostAdapter  # noqa: E402
from state_store import InvalidTransition, StateLock, read_state, run_state_path  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


class FakeAdapter(HostAdapter):
    def __init__(self, send_error=None):
        self.calls = []
        self.send_error = send_error

    def create_worker(self, prompt, project_id):
        self.calls.append(('create_worker', prompt, project_id))
        return {'threadId': 't-1'}

    def send_message(self, thread_id, message):
        self.calls.append(('send_message', thread_id, message))
        if self.send_error:
            raise self.send_error

    def read_thread(self, thread_id):
        self.calls.append(('read_thread', thread_id))
        return {'status': 'idle'}

    def wait_threads(self, thread_ids, timeout_seconds):
        self.calls.append(('wait_threads', thread_ids, timeout_seconds))
        return {'timedOut': False}

    def create_worktree(self, name):
        self.calls.append(('create_worktree', name))
        return {'root': name}

    def archive_worktree(self, root):
        self.calls.append(('archive_worktree', root))


NOW = datetime.now(timezone.utc).astimezone().isoformat()


class FailingSaveController(Controller):
    def __init__(self, *args, fail_on=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0
        self.fail_on = fail_on

    def save(self):
        self.save_calls += 1
        if self.save_calls == self.fail_on:
            raise OSError('save failed')
        super().save()


def make_state(run_id='run-1'):
    return {
        'schemaVersion': '1.0', 'runId': run_id,
        'createdAt': '2026-11-12T10:00:00+08:00',
        'updatedAt': '2026-11-12T10:00:00+08:00',
        'status': 'dispatching',
        'project': {
            'projectId': 'p1', 'hostId': 'local',
            'canonicalRepositoryRoot': '/repo', 'commonDirectory': '/repo/.git',
        },
        'integration': {
            'baseSha': 'a' * 40, 'branch': 'main', 'repoPath': '/repo',
        },
        'authorization': {'provenance': 'test', 'messagingScope': 'test'},
        'limits': {
            'workers': 2, 'initializationTimeoutSeconds': 300, 'replacementPerRole': 1,
        },
        'leader': {'threadId': 't-leader'},
        'resources': [],
        'mergeOrder': [],
        'integrationLog': [],
        'finalReview': {'status': 'pending', 'notes': ''},
        'cleanup': {
            'status': 'pending', 'preservedReports': [],
            'outstandingResources': [], 'notes': '',
        },
        'errors': [],
        'workers': [{
            'role': 'a', 'attempt': 1, 'state': 'ready', 'threadId': 't-a',
            'branch': 'tw/a-1', 'baseSha': 'a' * 40,
            'worktree': '/repo/worktree',
            'createdAt': '2026-11-12T10:00:00+08:00',
            'deadline': '2026-11-12T10:05:00+08:00',
            'lastObservation': '2026-11-12T10:00:00+08:00',
            'errors': [],
        }],
    }


with tempfile.TemporaryDirectory() as d:
    adapter = FakeAdapter()
    controller = Controller(adapter, d, 'run-1', make_state('run-1'))
    controller.save()
    check('state-file-written', run_state_path(d, 'run-1').is_file())

    controller.transition_run('observing')
    check('run-transition', read_state(run_state_path(d, 'run-1'))['status'] == 'observing')

    try:
        controller.transition_run('complete')
        check('invalid-run-transition-rejected', False)
    except InvalidTransition:
        check('invalid-run-transition-rejected', True)

    expected = {
        'common_dir': str(Path(d) / 'fake-common'),
        'branch': 'tw/a-1',
        'base': 'a' * 40,
        'other_roots': [Path(d) / 'leader'],
        'integration_head': 'a' * 40,
    }
    bad_report = {
        'cwd': '/repo/worktree',
        'worktree_root': '/repo/worktree',
        'git_common_dir': str(Path(d) / 'fake-common'),
        'branch': 'tw/a-1',
        'head': 'b' * 40,
        'status_porcelain': '',
        'role': 'a',
        'attempt': 1,
        'observedAt': NOW,
    }
    try:
        controller.dispatch('a', 1, 'bad dispatch', startup_report=bad_report, expected=expected)
        check('bad-startup-rejected', False)
    except RuntimeError:
        check('bad-startup-rejected', True)
    check('bad-startup-no-message',
          ('send_message', 't-a', 'bad dispatch') not in adapter.calls)

    good_report = {
        'cwd': '/repo/worktree',
        'worktree_root': '/repo/worktree',
        'git_common_dir': str(Path(d) / 'fake-common'),
        'branch': 'tw/a-1',
        'head': 'a' * 40,
        'status_porcelain': '',
        'role': 'a',
        'attempt': 1,
        'observedAt': NOW,
    }
    thread_id = controller.dispatch(
        'a', 1, 'do work', startup_report=good_report, expected=expected)
    check('dispatch-returns-thread-id', thread_id == 't-a')
    check('dispatch-calls-adapter', ('send_message', 't-a', 'do work') in adapter.calls)
    check('worker-dispatched',
          read_state(run_state_path(d, 'run-1'))['workers'][0]['state'] == 'dispatched')

    try:
        controller.dispatch('a', 1, 'again', startup_report=good_report, expected=expected)
        check('duplicate-dispatch-rejected', False)
    except RuntimeError:
        check('duplicate-dispatch-rejected', True)

    missing_controller = Controller(FakeAdapter(), d, 'run-missing', make_state('run-missing'))
    try:
        missing_controller.dispatch('a', 1, 'missing', startup_report=None, expected=expected)
        check('missing-startup-rejected', False)
    except ValueError:
        check('missing-startup-rejected', True)

    drift_expected = dict(expected, integration_head='b' * 40)
    drift_controller = Controller(FakeAdapter(), d, 'run-drift', make_state('run-drift'))
    try:
        drift_controller.dispatch(
            'a', 1, 'drift', startup_report=good_report, expected=drift_expected)
        check('integration-drift-rejected', False)
    except RuntimeError:
        check('integration-drift-rejected', True)

    team_state = make_state('run-team')
    team_state['workers'].append({
        'role': 'b', 'attempt': 1, 'state': 'pending', 'threadId': None,
        'branch': 'tw/b-1', 'baseSha': 'a' * 40,
        'worktree': '/repo/worktree-b',
        'createdAt': '2026-11-12T10:00:00+08:00',
        'deadline': '2026-11-12T10:05:00+08:00',
        'lastObservation': '2026-11-12T10:00:00+08:00',
        'errors': [],
    })
    team_controller = Controller(FakeAdapter(), d, 'run-team', team_state)
    try:
        team_controller.dispatch(
            'a', 1, 'team', startup_report=good_report, expected=expected)
        check('team-gate-rejected', False)
    except RuntimeError:
        check('team-gate-rejected', True)

    check('dispatch-intent-sent',
          read_state(run_state_path(d, 'run-1'))['workers'][0]['dispatchIntent']['result'] == 'sent')

    fail_adapter = FakeAdapter(send_error=RuntimeError('send failed'))
    fail_controller = Controller(fail_adapter, d, 'run-send-fail', make_state('run-send-fail'))
    try:
        fail_controller.dispatch(
            'a', 1, 'fail', startup_report=good_report, expected=expected)
        check('send-failure-raised', False)
    except RuntimeError:
        check('send-failure-raised', True)
    fail_saved = read_state(run_state_path(d, 'run-send-fail'))
    check('send-failure-unknown',
          fail_saved['workers'][0]['dispatchIntent']['result'] == 'unknown')

    wrong_role_controller = Controller(FakeAdapter(), d, 'run-wrong-role', make_state('run-wrong-role'))
    try:
        wrong_role_controller.dispatch(
            'a', 1, 'wrong role', startup_report=dict(good_report, role='b'),
            expected=expected)
        check('wrong-role-evidence-rejected', False)
    except RuntimeError:
        check('wrong-role-evidence-rejected', True)

    stale_controller = Controller(FakeAdapter(), d, 'run-stale-evidence', make_state('run-stale-evidence'))
    try:
        stale_controller.dispatch(
            'a', 1, 'stale',
            startup_report=dict(good_report, observedAt='2020-01-01T00:00:00Z'),
            expected=expected)
        check('stale-evidence-rejected', False)
    except RuntimeError:
        check('stale-evidence-rejected', True)

    missing_observed = dict(good_report)
    missing_observed.pop('observedAt')
    missing_observed_controller = Controller(
        FakeAdapter(), d, 'run-missing-observed', make_state('run-missing-observed'))
    try:
        missing_observed_controller.dispatch(
            'a', 1, 'missing observed', startup_report=missing_observed, expected=expected)
        check('missing-observed-at-rejected', False)
    except ValueError:
        check('missing-observed-at-rejected', True)

    persisted_branch_controller = Controller(
        FakeAdapter(), d, 'run-persisted-branch', make_state('run-persisted-branch'))
    try:
        persisted_branch_controller.dispatch(
            'a', 1, 'wrong branch',
            startup_report=dict(good_report, branch='wrong-branch'),
            expected=dict(expected, branch='wrong-branch'))
        check('persisted-branch-mismatch-rejected', False)
    except RuntimeError:
        check('persisted-branch-mismatch-rejected', True)

    stop_rework_state = make_state('run-stop-rework')
    stop_rework_state['workers'][0]['state'] = 'reported'
    stop_rework_controller = Controller(
        FakeAdapter(), d, 'run-stop-rework', stop_rework_state)
    stop_rework_controller.request_stop('a', 1)
    stop_rework_controller.confirm_stop(
        'a', 1, idle=True, final_answer_present=True, no_active_turn=True)
    stop_rework_controller.rework('a', 1, notes='rework')
    try:
        stop_rework_controller.replace_worker('a', 1)
        check('rework-invalidates-stop', False)
    except RuntimeError:
        check('rework-invalidates-stop', True)

    ab_state = make_state('run-ab')
    ab_state['workers'].append({
        'role': 'b', 'attempt': 1, 'state': 'ready', 'threadId': 't-b',
        'branch': 'tw/b-1', 'baseSha': 'a' * 40,
        'worktree': '/repo/worktree-b',
        'createdAt': '2026-11-12T10:00:00+08:00',
        'deadline': '2026-11-12T10:05:00+08:00',
        'lastObservation': '2026-11-12T10:00:00+08:00',
        'errors': [],
    })
    ab_controller = Controller(FakeAdapter(), d, 'run-ab', ab_state)
    ab_controller.dispatch('a', 1, 'a first', startup_report=good_report, expected=expected)
    expected_b = dict(expected, branch='tw/b-1')
    good_b = dict(good_report, role='b', branch='tw/b-1',
                  worktree_root='/repo/worktree-b', cwd='/repo/worktree-b')
    ab_controller.dispatch('b', 1, 'b second', startup_report=good_b, expected=expected_b)
    ab_saved = read_state(run_state_path(d, 'run-ab'))
    check('sequential-dispatch-a-b',
          [w['state'] for w in ab_saved['workers']] == ['dispatched', 'dispatched'])
    check('startup-evidence-persisted',
          ab_saved['workers'][0]['startupEvidence']['role'] == 'a')

    aborted_state = make_state('run-aborted')
    aborted_state['status'] = 'aborted'
    aborted_controller = Controller(FakeAdapter(), d, 'run-aborted', aborted_state)
    try:
        aborted_controller.dispatch(
            'a', 1, 'aborted', startup_report=good_report, expected=expected)
        check('aborted-dispatch-rejected', False)
    except RuntimeError:
        check('aborted-dispatch-rejected', True)

    stopping_state = make_state('run-stopping')
    stopping_state['workers'][0]['stopStatus'] = 'requested'
    stopping_controller = Controller(FakeAdapter(), d, 'run-stopping', stopping_state)
    try:
        stopping_controller.dispatch(
            'a', 1, 'stopping', startup_report=good_report, expected=expected)
        check('stopping-dispatch-rejected', False)
    except RuntimeError:
        check('stopping-dispatch-rejected', True)

    pending_state = make_state('run-pending')
    pending_state['workers'][0]['dispatchIntent'] = {
        'dispatchId': 'old', 'message': 'old',
        'at': '2026-11-12T10:00:00+08:00', 'result': 'pending',
    }
    pending_controller = Controller(FakeAdapter(), d, 'run-pending', pending_state)
    try:
        pending_controller.dispatch(
            'a', 1, 'pending', startup_report=good_report, expected=expected)
        check('pending-dispatch-rejected', False)
    except RuntimeError:
        check('pending-dispatch-rejected', True)

    aborted_create_adapter = FakeAdapter()
    aborted_create_controller = Controller(
        aborted_create_adapter, d, 'run-aborted-create', aborted_state)
    try:
        aborted_create_controller.create_worker('prompt', 'p1')
        check('aborted-create-rejected', False)
    except RuntimeError:
        check('aborted-create-rejected', True)
    check('aborted-create-no-call', not aborted_create_adapter.calls)

    old_attempt_state = make_state('run-old-attempt')
    old_attempt_state['workers'][0]['state'] = 'cancelled'
    old_attempt_state['workers'].append({
        'role': 'a', 'attempt': 2, 'state': 'ready', 'threadId': 't-a2',
        'branch': 'tw/a-2', 'baseSha': 'a' * 40,
        'worktree': '/repo/worktree-a2',
        'createdAt': '2026-11-12T10:00:00+08:00',
        'deadline': '2026-11-12T10:05:00+08:00',
        'lastObservation': '2026-11-12T10:00:00+08:00',
        'errors': [],
    })
    old_attempt_controller = Controller(FakeAdapter(), d, 'run-old-attempt', old_attempt_state)
    expected_a2 = dict(expected, branch='tw/a-2')
    good_a2 = dict(good_report, attempt=2, branch='tw/a-2',
                   worktree_root='/repo/worktree-a2', cwd='/repo/worktree-a2')
    old_attempt_controller.dispatch(
        'a', 2, 'a2', startup_report=good_a2, expected=expected_a2)
    check('old-attempt-does-not-block',
          read_state(run_state_path(d, 'run-old-attempt'))['workers'][1]['state'] == 'dispatched')

    running_b_state = make_state('run-running-b')
    running_b_state['workers'][0]['state'] = 'running'
    running_b_state['workers'].append({
        'role': 'b', 'attempt': 1, 'state': 'ready', 'threadId': 't-b',
        'branch': 'tw/b-1', 'baseSha': 'a' * 40,
        'worktree': '/repo/worktree-b',
        'createdAt': '2026-11-12T10:00:00+08:00',
        'deadline': '2026-11-12T10:05:00+08:00',
        'lastObservation': '2026-11-12T10:00:00+08:00',
        'errors': [],
    })
    running_b_controller = Controller(FakeAdapter(), d, 'run-running-b', running_b_state)
    expected_b2 = dict(expected, branch='tw/b-1')
    good_b2 = dict(good_report, role='b', branch='tw/b-1',
                   worktree_root='/repo/worktree-b', cwd='/repo/worktree-b')
    running_b_controller.dispatch(
        'b', 1, 'b', startup_report=good_b2, expected=expected_b2)
    check('running-a-does-not-block-b',
          read_state(run_state_path(d, 'run-running-b'))['workers'][1]['state'] == 'dispatched')

    save_fail_adapter = FakeAdapter()
    save_fail_controller = FailingSaveController(
        save_fail_adapter, d, 'run-save-fail', make_state('run-save-fail'), fail_on=1)
    try:
        save_fail_controller.dispatch(
            'a', 1, 'save before send', startup_report=good_report, expected=expected)
        check('pre-send-save-failure-rejected', False)
    except OSError:
        check('pre-send-save-failure-rejected', True)
    check('pre-send-save-no-message',
          not any(call[0] == 'send_message' for call in save_fail_adapter.calls))

    post_adapter = FakeAdapter()
    post_controller = FailingSaveController(
        post_adapter, d, 'run-post-save-fail', make_state('run-post-save-fail'), fail_on=2)
    try:
        post_controller.dispatch(
            'a', 1, 'save after send', startup_report=good_report, expected=expected)
        check('post-send-save-failure-raised', False)
    except OSError:
        check('post-send-save-failure-raised', True)
    check('post-send-message-sent',
          ('send_message', 't-a', 'save after send') in post_adapter.calls)
    post_saved = read_state(run_state_path(d, 'run-post-save-fail'))
    check('post-send-intent-pending',
          post_saved['workers'][0]['dispatchIntent']['result'] == 'pending')

    try:
        controller.replace_worker('a', 1, replacement_limit=1)
        check('replace-without-stop-rejected', False)
    except RuntimeError:
        check('replace-without-stop-rejected', True)

    controller.request_stop('a', 1)
    try:
        controller.confirm_stop('a', 1, idle=False, final_answer_present=True, no_active_turn=True)
        check('incomplete-stop-rejected', False)
    except RuntimeError:
        check('incomplete-stop-rejected', True)
    controller.confirm_stop('a', 1, idle=True, final_answer_present=True, no_active_turn=True)
    check('stop-confirmed',
          read_state(run_state_path(d, 'run-1'))['workers'][0]['stopStatus'] == 'confirmed')
    try:
        controller.replace_worker('a', 1, replacement_limit=1)
        check('replace-first-ok', True)
    except RuntimeError:
        check('replace-first-ok', False)

    rework_state = make_state('run-rework')
    rework_state['workers'][0]['state'] = 'reported'
    rework_controller = Controller(FakeAdapter(), d, 'run-rework', rework_state)
    rework_controller.rework('a', 1, notes='fix review findings')
    rework_saved = read_state(run_state_path(d, 'run-rework'))
    check('rework-transition', rework_saved['workers'][0]['state'] == 'running')
    check('rework-count', rework_saved['workers'][0]['reworkCount'] == 1)

    state2 = make_state('run-2')
    state2['workers'][0]['attempt'] = 2
    state2['workers'][0]['stopStatus'] = 'confirmed'
    controller2 = Controller(FakeAdapter(), d, 'run-2', state2)
    try:
        controller2.replace_worker('a', 2, replacement_limit=1)
        check('replace-over-limit-rejected', False)
    except RuntimeError:
        check('replace-over-limit-rejected', True)

    plan_result = controller.write_plan(
        {'phases': ['execute', 'review', 'commit']}, update_plan_available=False)
    check('plan-downgrade-state-file', plan_result == 'state-file')
    saved = read_state(run_state_path(d, 'run-1'))
    check('plan-written-to-state',
          saved['plan']['phases'] == ['execute', 'review', 'commit'])
    check('plan-update-plan-available',
          controller.write_plan({'x': 1}, update_plan_available=True) == 'update_plan')

    stale_a = Controller(FakeAdapter(), d, 'run-stale', make_state('run-stale'))
    stale_b = Controller(FakeAdapter(), d, 'run-stale', make_state('run-stale'))
    stale_a.save()
    try:
        stale_b.save()
        check('stale-revision-rejected', False)
    except StaleStateError:
        check('stale-revision-rejected', True)

    write_fail_controller = Controller(
        FakeAdapter(), d, 'run-write-fail', make_state('run-write-fail'))
    write_fail_controller.save()
    revision_before = write_fail_controller.state['revision']
    with patch('controller.write_state_atomic', side_effect=OSError('disk full')):
        try:
            write_fail_controller.save()
            check('write-failure-raised', False)
        except OSError:
            check('write-failure-raised', True)
    check('write-failure-no-revision-bump',
          write_fail_controller.state['revision'] == revision_before)
    write_fail_controller.save()
    check('write-retry-succeeds',
          read_state(run_state_path(d, 'run-write-fail'))['revision'] == revision_before + 1)

    mismatch_controller = Controller(
        FakeAdapter(), d, 'run-mismatch', make_state('wrong-run'))
    try:
        mismatch_controller.save()
        check('run-id-mismatch-rejected', False)
    except ValueError:
        check('run-id-mismatch-rejected', True)

    false_complete = make_state('run-false-complete')
    false_complete['status'] = 'complete'
    false_complete['finalReview'] = {'status': 'complete', 'notes': ''}
    false_complete['cleanup'] = {
        'status': 'complete', 'preservedReports': [],
        'outstandingResources': [], 'notes': '',
    }
    false_complete['workers'][0]['state'] = 'running'
    try:
        Controller(FakeAdapter(), d, 'run-false-complete', false_complete).save()
        check('false-complete-rejected', False)
    except ValueError:
        check('false-complete-rejected', True)

    resolve_state = make_state('run-resolve')
    resolve_state['workers'][0]['dispatchIntent'] = {
        'dispatchId': 'd1', 'message': 'task',
        'at': '2026-11-12T10:00:00+08:00', 'result': 'unknown',
    }
    resolve_controller = Controller(FakeAdapter(), d, 'run-resolve', resolve_state)
    resolve_controller.resolve_dispatch('a', 1, 'sent')
    resolved = read_state(run_state_path(d, 'run-resolve'))
    check('resolve-sent-dispatched', resolved['workers'][0]['state'] == 'dispatched')
    check('resolve-sent-resolvedAt', 'resolvedAt' in resolved['workers'][0]['dispatchIntent'])

    not_sent_state = make_state('run-not-sent')
    not_sent_state['workers'][0]['dispatchIntent'] = {
        'dispatchId': 'd2', 'message': 'task',
        'at': '2026-11-12T10:00:00+08:00', 'result': 'unknown',
    }
    not_sent_controller = Controller(FakeAdapter(), d, 'run-not-sent', not_sent_state)
    not_sent_controller.resolve_dispatch('a', 1, 'not_sent')
    not_sent_saved = read_state(run_state_path(d, 'run-not-sent'))
    check('resolve-not-sent-ready', not_sent_saved['workers'][0]['state'] == 'ready')

    unknown_state = make_state('run-unknown')
    unknown_state['workers'][0]['dispatchIntent'] = {
        'dispatchId': 'd3', 'message': 'task',
        'at': '2026-11-12T10:00:00+08:00', 'result': 'pending',
    }
    unknown_controller = Controller(FakeAdapter(), d, 'run-unknown', unknown_state)
    unknown_controller.resolve_dispatch('a', 1, 'unknown')
    unknown_saved = read_state(run_state_path(d, 'run-unknown'))
    check('resolve-unknown-stays-pending',
          unknown_saved['workers'][0]['dispatchIntent']['result'] == 'unknown')

    history_complete = make_state('run-history-complete')
    history_complete['workers'][0]['state'] = 'cancelled'
    history_complete['workers'][0]['stopStatus'] = 'confirmed'
    history_complete['workers'].append({
        'role': 'a', 'attempt': 2, 'state': 'archived', 'threadId': 't-a2',
        'branch': 'tw/a-2', 'baseSha': 'a' * 40,
        'worktree': '/repo/worktree-a2',
        'createdAt': '2026-11-12T10:00:00+08:00',
        'deadline': '2026-11-12T10:05:00+08:00',
        'lastObservation': '2026-11-12T10:00:00+08:00',
        'errors': [],
    })
    history_complete['status'] = 'complete'
    history_complete['finalReview'] = {'status': 'complete', 'notes': ''}
    history_complete['cleanup'] = {
        'status': 'complete', 'preservedReports': [],
        'outstandingResources': [], 'notes': '',
    }
    try:
        Controller(FakeAdapter(), d, 'run-history-complete', history_complete).save()
        check('history-cancelled-does-not-block-complete', True)
    except ValueError:
        check('history-cancelled-does-not-block-complete', False)

    old_dispatch_state = make_state('run-old-dispatch')
    old_dispatch_state['workers'][0]['state'] = 'ready'
    old_dispatch_state['workers'].append({
        'role': 'a', 'attempt': 2, 'state': 'ready', 'threadId': 't-a2',
        'branch': 'tw/a-2', 'baseSha': 'a' * 40,
        'worktree': '/repo/worktree-a2',
        'createdAt': '2026-11-12T10:00:00+08:00',
        'deadline': '2026-11-12T10:05:00+08:00',
        'lastObservation': '2026-11-12T10:00:00+08:00',
        'errors': [],
    })
    old_dispatch_controller = Controller(
        FakeAdapter(), d, 'run-old-dispatch', old_dispatch_state)
    try:
        old_dispatch_controller.dispatch(
            'a', 1, 'old', startup_report=good_report, expected=expected)
        check('old-attempt-dispatch-rejected', False)
    except RuntimeError:
        check('old-attempt-dispatch-rejected', True)

    future_controller = Controller(
        FakeAdapter(), d, 'run-future', make_state('run-future'))
    try:
        future_controller.dispatch(
            'a', 1, 'future',
            startup_report=dict(good_report, observedAt='2099-01-01T00:00:00+00:00'),
            expected=expected)
        check('future-evidence-rejected', False)
    except RuntimeError:
        check('future-evidence-rejected', True)

    no_worktree_state = make_state('run-no-worktree')
    no_worktree_state['workers'][0].pop('worktree')
    no_worktree_controller = Controller(
        FakeAdapter(), d, 'run-no-worktree', no_worktree_state)
    try:
        no_worktree_controller.dispatch(
            'a', 1, 'no worktree', startup_report=good_report, expected=expected)
        check('missing-worktree-rejected', False)
    except RuntimeError:
        check('missing-worktree-rejected', True)

    report_state = make_state('run-report')
    report_state['workers'][0]['state'] = 'running'
    report_state['workers'][0]['activityRevision'] = 1
    report_state['workers'][0]['branchTip'] = 'b' * 40
    report_controller = Controller(FakeAdapter(), d, 'run-report', report_state)
    report_payload = {
        'threadId': 't-a', 'branch': 'tw/a-1',
        'head': 'b' * 40, 'reportSha': 'c' * 64,
        'taskRevision': 1, 'baseIsAncestor': True,
    }
    report_controller.accept_report('a', 1, report_payload)
    report_saved = read_state(run_state_path(d, 'run-report'))
    check('accept-report-recorded', report_saved['workers'][0]['state'] == 'reported')
    check('accept-report-sha', report_saved['workers'][0]['report']['reportSha'] == 'c' * 64)

    bad_sha_state = make_state('run-bad-sha')
    bad_sha_state['workers'][0]['state'] = 'running'
    bad_sha_state['workers'][0]['activityRevision'] = 1
    bad_sha_state['workers'][0]['branchTip'] = 'd' * 40
    bad_sha_controller = Controller(FakeAdapter(), d, 'run-bad-sha', bad_sha_state)
    try:
        bad_sha_controller.accept_report('a', 1, report_payload)
        check('report-head-mismatch-rejected', False)
    except RuntimeError:
        check('report-head-mismatch-rejected', True)

    old_report_state = make_state('run-old-report')
    old_report_state['workers'][0]['state'] = 'running'
    old_report_state['workers'][0]['activityRevision'] = 1
    old_report_state['workers'][0]['branchTip'] = 'b' * 40
    old_report_controller = Controller(FakeAdapter(), d, 'run-old-report', old_report_state)
    try:
        old_report_controller.accept_report('a', 1, dict(report_payload, taskRevision=0))
        check('old-report-rejected', False)
    except RuntimeError:
        check('old-report-rejected', True)

    report_controller.integrate('a', 1, 'e' * 40, 'tests passed')
    integrated_saved = read_state(run_state_path(d, 'run-report'))
    check('integrate-worker-state', integrated_saved['workers'][0]['state'] == 'integrated')
    check('integration-log-recorded',
          integrated_saved['integrationLog'][0]['result'] == 'merged')

    no_log_state = make_state('run-no-log')
    no_log_state['workers'][0]['state'] = 'reported'
    no_log_state['workers'][0]['report'] = dict(
        report_payload, acceptedAt='2026-11-12T10:00:00+08:00')
    no_log_controller = Controller(FakeAdapter(), d, 'run-no-log', no_log_state)
    try:
        no_log_controller.transition_worker('a', 1, 'integrated')
        check('integrated-without-log-rejected', False)
    except RuntimeError:
        check('integrated-without-log-rejected', True)

    fail_integration_state = make_state('run-fail-integration')
    fail_integration_state['workers'][0]['state'] = 'running'
    fail_integration_state['workers'][0]['activityRevision'] = 1
    fail_integration_state['workers'][0]['branchTip'] = 'b' * 40
    fail_integration_controller = Controller(
        FakeAdapter(), d, 'run-fail-integration', fail_integration_state)
    fail_integration_controller.accept_report('a', 1, report_payload)
    try:
        fail_integration_controller.integrate('a', 1, None, 'FAIL: tests failed')
        check('integration-failure-raised', False)
    except RuntimeError:
        check('integration-failure-raised', True)
    fail_saved = read_state(run_state_path(d, 'run-fail-integration'))
    check('integration-failure-recorded',
          fail_saved['integrationLog'][0]['result'] == 'failed')
    check('integration-failure-worker-reported',
          fail_saved['workers'][0]['state'] == 'reported')

    cleanup_state = make_state('run-cleanup-pending')
    cleanup_state['status'] = 'cleanup_pending'
    cleanup_controller = Controller(FakeAdapter(), d, 'run-cleanup-pending', cleanup_state)
    try:
        cleanup_controller.dispatch(
            'a', 1, 'cleanup', startup_report=good_report, expected=expected)
        check('cleanup-pending-dispatch-rejected', False)
    except RuntimeError:
        check('cleanup-pending-dispatch-rejected', True)

    no_auth_state = make_state('run-no-auth')
    no_auth_state['authorization'] = {'provenance': '', 'messagingScope': ''}
    no_auth_adapter = FakeAdapter()
    no_auth_controller = Controller(no_auth_adapter, d, 'run-no-auth', no_auth_state)
    try:
        no_auth_controller.create_worker('prompt', 'p1')
        check('missing-authorization-create-rejected', False)
    except RuntimeError:
        check('missing-authorization-create-rejected', True)
    check('missing-authorization-no-adapter-call', not no_auth_adapter.calls)

    bad_state = make_state('run-bad')
    bad_state['status'] = 'not-a-status'
    bad_controller = Controller(FakeAdapter(), d, 'run-bad', bad_state)
    try:
        bad_controller.save()
        check('invalid-state-save-rejected', False)
    except ValueError:
        check('invalid-state-save-rejected', True)
    lock = StateLock(run_state_path(d, 'run-bad').with_suffix('.lock'))
    lock.acquire()
    lock.release()
    check('lock-released-after-save-failure', True)

    try:
        Controller(object(), d, 'run-2', make_state('run-2'))
        check('non-adapter-rejected', False)
    except TypeError:
        check('non-adapter-rejected', True)

    unsupported = UnsupportedHostAdapter()
    try:
        unsupported.create_worker('x', 'p')
        check('unsupported-adapter-rejects', False)
    except NotImplementedError:
        check('unsupported-adapter-rejects', True)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Fake adapter controller logic only; not a real host run',
}, ensure_ascii=False, indent=2))
