"""用 fake adapter 验证项目匹配、create 超时与可选工具降级。"""
import json
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller  # noqa: E402
from host_adapter import HostAdapter  # noqa: E402
from project_resolver import ProjectResolutionError, resolve_project  # noqa: E402
from state_store import read_state, run_state_path  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


class FaultAdapter(HostAdapter):
    capabilities = {
        'update_plan': True,
        'wait_threads': True,
        'create_timeout': False,
    }

    def __init__(self, create_error=None):
        self.calls = []
        self.create_error = create_error

    def create_worker(self, prompt, project_id):
        self.calls.append(('create_worker', prompt, project_id))
        if self.create_error:
            raise self.create_error
        return {'threadId': 't-1'}

    def send_message(self, thread_id, message):
        self.calls.append(('send_message', thread_id, message))

    def read_thread(self, thread_id):
        return {'status': 'idle'}

    def wait_threads(self, thread_ids, timeout_seconds):
        self.calls.append(('wait_threads', thread_ids, timeout_seconds))
        return {'timedOut': False}

    def create_worktree(self, name):
        return {'root': name}

    def archive_worktree(self, root):
        return None


def make_state(run_id):
    return {
        'schemaVersion': '1.0', 'runId': run_id,
        'status': 'initializing', 'workers': [],
    }


with tempfile.TemporaryDirectory(prefix='threadweave-host-fault-') as d:
    root = Path(d)
    repo = root / 'repo'
    repo.mkdir()

    unique = [{'projectId': 'p1', 'path': str(repo), 'isGitRepository': True}]
    check('unique-match-returns', resolve_project(unique, repo)['projectId'] == 'p1')

    multiple = [
        {'projectId': 'p1', 'path': str(repo), 'isGitRepository': True},
        {'projectId': 'p2', 'path': str(repo), 'isGitRepository': True},
    ]
    try:
        resolve_project(multiple, repo)
        check('multiple-matches-rejected', False)
    except ProjectResolutionError as exc:
        check('multiple-matches-rejected', 'multiple' in str(exc))

    zero = [{'projectId': 'p1', 'path': str(root / 'other'), 'isGitRepository': True}]
    try:
        resolve_project(zero, repo)
        check('zero-matches-rejected', False)
    except ProjectResolutionError as exc:
        check('zero-matches-rejected', 'no matching' in str(exc))

    non_git = [{'projectId': 'p1', 'path': str(repo), 'isGitRepository': False}]
    try:
        resolve_project(non_git, repo)
        check('non-git-ignored', False)
    except ProjectResolutionError:
        check('non-git-ignored', True)

    timeout_adapter = FaultAdapter(create_error=TimeoutError('create timed out'))
    timeout_controller = Controller(timeout_adapter, root, 'run-timeout', make_state('run-timeout'))
    try:
        timeout_controller.create_worker('prompt', 'p1')
        check('create-timeout-raised', False)
    except TimeoutError:
        check('create-timeout-raised', True)
    saved = read_state(run_state_path(root, 'run-timeout'))
    check('create-timeout-recorded', saved['lastCreateError']['code'] == 'create_timeout')
    check('create-timeout-no-message',
          not any(call[0] == 'send_message' for call in timeout_adapter.calls))

    downgrade_adapter = FaultAdapter()
    downgrade_adapter.capabilities = {
        'update_plan': False, 'wait_threads': False, 'create_timeout': False,
    }
    downgrade = Controller(downgrade_adapter, root, 'run-downgrade', make_state('run-downgrade'))
    check('update-plan-downgrade', downgrade.write_plan({'phases': ['execute']}) == 'state-file')
    saved = read_state(run_state_path(root, 'run-downgrade'))
    check('plan-written-to-state', saved['plan']['phases'] == ['execute'])
    wait_result = downgrade.wait_for_workers(['t-1'], 30)
    check('wait-threads-downgrade', wait_result['mode'] == 'bounded-status')

    normal_adapter = FaultAdapter()
    normal = Controller(normal_adapter, root, 'run-normal', make_state('run-normal'))
    check('update-plan-available', normal.write_plan({'x': 1}) == 'update_plan')
    check('wait-threads-available', normal.wait_for_workers(['t-1'], 30) == {'timedOut': False})
    check('wait-threads-called', ('wait_threads', ['t-1'], 30) in normal_adapter.calls)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Fake adapter host-fault scenarios only; not a real host run',
}, ensure_ascii=False, indent=2))
