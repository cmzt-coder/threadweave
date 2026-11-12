"""用 fake adapter 验证控制器纯逻辑编排，不代表真实宿主运行。"""
import json
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller  # noqa: E402
from host_adapter import HostAdapter, UnsupportedHostAdapter  # noqa: E402
from state_store import InvalidTransition, read_state, run_state_path  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


class FakeAdapter(HostAdapter):
    def __init__(self):
        self.calls = []

    def create_worker(self, prompt, project_id):
        self.calls.append(('create_worker', prompt, project_id))
        return {'threadId': 't-1'}

    def send_message(self, thread_id, message):
        self.calls.append(('send_message', thread_id, message))

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


def make_state():
    return {
        'schemaVersion': '1.0', 'runId': 'run-1', 'status': 'dispatching',
        'workers': [{'role': 'a', 'attempt': 1, 'state': 'ready', 'threadId': 't-a'}],
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
    }
    bad_report = {
        'cwd': str(Path(d) / 'worker'),
        'worktree_root': str(Path(d) / 'worker'),
        'git_common_dir': str(Path(d) / 'fake-common'),
        'branch': 'tw/a-1',
        'head': 'b' * 40,
        'status_porcelain': '',
    }
    try:
        controller.dispatch('a', 1, 'bad dispatch', startup_report=bad_report, expected=expected)
        check('bad-startup-rejected', False)
    except RuntimeError:
        check('bad-startup-rejected', True)
    check('bad-startup-no-message',
          ('send_message', 't-a', 'bad dispatch') not in adapter.calls)

    thread_id = controller.dispatch('a', 1, 'do work')
    check('dispatch-returns-thread-id', thread_id == 't-a')
    check('dispatch-calls-adapter', ('send_message', 't-a', 'do work') in adapter.calls)
    check('worker-dispatched',
          read_state(run_state_path(d, 'run-1'))['workers'][0]['state'] == 'dispatched')

    try:
        controller.dispatch('a', 1, 'again')
        check('duplicate-dispatch-rejected', False)
    except RuntimeError:
        check('duplicate-dispatch-rejected', True)

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
