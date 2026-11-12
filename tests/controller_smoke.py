"""用 fake adapter 验证控制器纯逻辑编排，不代表真实宿主运行。"""
import json
import sys
import tempfile
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


def make_state():
    return {
        'schemaVersion': '1.0', 'runId': 'run-1',
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
            'createdAt': '2026-11-12T10:00:00+08:00',
            'deadline': '2026-11-12T10:05:00+08:00',
            'lastObservation': '2026-11-12T10:00:00+08:00',
            'errors': [],
        }],
    }


with tempfile.TemporaryDirectory() as d:
    adapter = FakeAdapter()
    controller = Controller(adapter, d, 'run-1', make_state())
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
        'cwd': str(Path(d) / 'worker'),
        'worktree_root': str(Path(d) / 'worker'),
        'git_common_dir': str(Path(d) / 'fake-common'),
        'branch': 'tw/a-1',
        'head': 'b' * 40,
        'status_porcelain': '',
        'role': 'a',
        'attempt': 1,
    }
    try:
        controller.dispatch('a', 1, 'bad dispatch', startup_report=bad_report, expected=expected)
        check('bad-startup-rejected', False)
    except RuntimeError:
        check('bad-startup-rejected', True)
    check('bad-startup-no-message',
          ('send_message', 't-a', 'bad dispatch') not in adapter.calls)

    good_report = {
        'cwd': str(Path(d) / 'worker'),
        'worktree_root': str(Path(d) / 'worker'),
        'git_common_dir': str(Path(d) / 'fake-common'),
        'branch': 'tw/a-1',
        'head': 'a' * 40,
        'status_porcelain': '',
        'role': 'a',
        'attempt': 1,
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

    missing_controller = Controller(FakeAdapter(), d, 'run-missing', make_state())
    try:
        missing_controller.dispatch('a', 1, 'missing', startup_report=None, expected=expected)
        check('missing-startup-rejected', False)
    except ValueError:
        check('missing-startup-rejected', True)

    drift_expected = dict(expected, integration_head='b' * 40)
    drift_controller = Controller(FakeAdapter(), d, 'run-drift', make_state())
    try:
        drift_controller.dispatch(
            'a', 1, 'drift', startup_report=good_report, expected=drift_expected)
        check('integration-drift-rejected', False)
    except RuntimeError:
        check('integration-drift-rejected', True)

    team_state = make_state()
    team_state['workers'].append({
        'role': 'b', 'attempt': 1, 'state': 'pending', 'threadId': None,
        'branch': 'tw/b-1', 'baseSha': 'a' * 40,
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
    fail_controller = Controller(fail_adapter, d, 'run-send-fail', make_state())
    try:
        fail_controller.dispatch(
            'a', 1, 'fail', startup_report=good_report, expected=expected)
        check('send-failure-raised', False)
    except RuntimeError:
        check('send-failure-raised', True)
    fail_saved = read_state(run_state_path(d, 'run-send-fail'))
    check('send-failure-unknown',
          fail_saved['workers'][0]['dispatchIntent']['result'] == 'unknown')

    wrong_role_controller = Controller(FakeAdapter(), d, 'run-wrong-role', make_state())
    try:
        wrong_role_controller.dispatch(
            'a', 1, 'wrong role', startup_report=dict(good_report, role='b'),
            expected=expected)
        check('wrong-role-evidence-rejected', False)
    except RuntimeError:
        check('wrong-role-evidence-rejected', True)

    stale_controller = Controller(FakeAdapter(), d, 'run-stale-evidence', make_state())
    try:
        stale_controller.dispatch(
            'a', 1, 'stale',
            startup_report=dict(good_report, observedAt='2020-01-01T00:00:00Z'),
            expected=expected)
        check('stale-evidence-rejected', False)
    except RuntimeError:
        check('stale-evidence-rejected', True)

    ab_state = make_state()
    ab_state['workers'].append({
        'role': 'b', 'attempt': 1, 'state': 'ready', 'threadId': 't-b',
        'branch': 'tw/b-1', 'baseSha': 'a' * 40,
        'createdAt': '2026-11-12T10:00:00+08:00',
        'deadline': '2026-11-12T10:05:00+08:00',
        'lastObservation': '2026-11-12T10:00:00+08:00',
        'errors': [],
    })
    ab_controller = Controller(FakeAdapter(), d, 'run-ab', ab_state)
    ab_controller.dispatch('a', 1, 'a first', startup_report=good_report, expected=expected)
    expected_b = dict(expected, branch='tw/b-1')
    good_b = dict(good_report, role='b', branch='tw/b-1')
    ab_controller.dispatch('b', 1, 'b second', startup_report=good_b, expected=expected_b)
    ab_saved = read_state(run_state_path(d, 'run-ab'))
    check('sequential-dispatch-a-b',
          [w['state'] for w in ab_saved['workers']] == ['dispatched', 'dispatched'])
    check('startup-evidence-persisted',
          ab_saved['workers'][0]['startupEvidence']['role'] == 'a')

    save_fail_adapter = FakeAdapter()
    save_fail_controller = FailingSaveController(
        save_fail_adapter, d, 'run-save-fail', make_state(), fail_on=1)
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
        post_adapter, d, 'run-post-save-fail', make_state(), fail_on=2)
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
        check('replace-first-ok', True)
    except RuntimeError:
        check('replace-first-ok', False)

    state2 = make_state()
    state2['workers'][0]['attempt'] = 2
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

    stale_a = Controller(FakeAdapter(), d, 'run-stale', make_state())
    stale_b = Controller(FakeAdapter(), d, 'run-stale', make_state())
    stale_a.save()
    try:
        stale_b.save()
        check('stale-revision-rejected', False)
    except StaleStateError:
        check('stale-revision-rejected', True)

    bad_state = make_state()
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
        Controller(object(), d, 'run-2', make_state())
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
