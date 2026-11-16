"""验证 O06a setup 配置、超时、失败阻断和幂等复用。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller  # noqa: E402
from host_adapter import UnsupportedHostAdapter  # noqa: E402
from setup import SetupConfigError, load_setup_config  # noqa: E402
from state_store import read_state, run_state_path  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def write_config(root, argv, timeout=3, cwd='.', idempotent=True):
    config = {'setup': {
        'version': 1, 'argv': argv, 'cwd': cwd,
        'timeoutSeconds': timeout, 'failurePolicy': 'block',
        'idempotent': idempotent,
    }}
    (root / 'threadweave.config.json').write_text(
        json.dumps(config), encoding='utf-8')


with tempfile.TemporaryDirectory(prefix='threadweave-setup-') as d:
    root = Path(d) / 'repo'
    wt = Path(d) / 'worker'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'tracked.txt').write_text('base\n', encoding='utf-8')
    git(root, 'add', 'tracked.txt')
    git(root, 'commit', '-m', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    (root / '.git' / 'info').mkdir(exist_ok=True)
    (root / '.git' / 'info' / 'exclude').write_text(
        '.setup-count\n', encoding='utf-8')
    git(root, 'worktree', 'add', '-b', 'tw/a', str(wt), base)

    safe_argv = [
        sys.executable, '-c',
        "from pathlib import Path; p=Path('.setup-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    ]
    write_config(root, safe_argv)
    loaded = load_setup_config(root)
    check('setup-config-loaded', loaded['timeoutSeconds'] == 3)
    check('setup-config-hash', len(loaded['configHash']) == 64)
    check('setup-config-idempotent', loaded['idempotent'] is True)

    state = json.loads((project / 'references/state.example.json').read_text())
    state.update(runId='run-setup', status='initializing', revision=0)
    state['project'].update(
        canonicalRepositoryRoot=str(root), commonDirectory=str((root / '.git').resolve()))
    state['integration'].update(baseSha=base, branch='main', repoPath=str(root))
    state['workers'] = [{
        'role': 'a', 'attempt': 1, 'state': 'validating', 'threadId': 't-a',
        'branch': 'tw/a', 'baseSha': base, 'worktree': str(wt),
        'createdAt': '2026-11-16T00:00:00+08:00',
        'deadline': '2026-11-16T00:10:00+08:00',
        'lastObservation': '2026-11-16T00:00:00+08:00', 'errors': [],
    }]
    state['resources'] = []
    state['mergeOrder'] = []
    state['integrationLog'] = []
    state['finalReview'] = {'status': 'pending', 'notes': ''}
    state['cleanup'] = {'status': 'pending', 'preservedReports': [],
                        'outstandingResources': [], 'notes': ''}
    state['errors'] = []
    controller = Controller(UnsupportedHostAdapter(), root, 'run-setup', state)
    controller.save()
    first = controller.run_worker_setup('a', 1)
    check('setup-passes', first['status'] == 'passed')
    check('setup-count-once', (wt / '.setup-count').read_text() == '1')
    saved = read_state(run_state_path(root, 'run-setup'))
    check('setup-status-persisted', saved['workers'][0]['setupStatus'] == 'passed')
    second = controller.run_worker_setup('a', 1)
    check('setup-reused', second.get('reused') is True)
    check('setup-no-double-run', (wt / '.setup-count').read_text() == '1')

    write_config(root, [sys.executable, '-c', 'import time; time.sleep(2)'], timeout=1)
    timeout_result = controller.run_worker_setup('a', 1)
    check('setup-timeout-recorded', timeout_result['status'] == 'failed'
          and timeout_result['timedOut'] is True)
    saved = read_state(run_state_path(root, 'run-setup'))
    check('setup-timeout-error-source',
          saved['errors'][-1]['source'] == 'workspace_setup'
          and saved['errors'][-1]['code'] == 'setup_timeout')

    write_config(root, [sys.executable, '-c', "from pathlib import Path; Path('tracked.txt').write_text('dirty\\n')"], timeout=3)
    dirty_result = controller.run_worker_setup('a', 1)
    check('setup-dirty-blocked', dirty_result['status'] == 'failed'
          and 'dirty_worktree' in dirty_result['checks'])
    saved = read_state(run_state_path(root, 'run-setup'))
    check('setup-dirty-error', saved['errors'][-1]['code'] == 'setup_dirty_worktree')
    check('setup-dirty-no-auto-reset', (wt / 'tracked.txt').read_text() == 'dirty\n')

    bad = root / 'threadweave.config.json'
    bad.write_text(json.dumps({'setup': {
        'version': 1, 'argv': [sys.executable, '-V'], 'cwd': '../outside',
        'timeoutSeconds': 3, 'failurePolicy': 'block', 'idempotent': True,
    }}), encoding='utf-8')
    try:
        load_setup_config(root)
        check('setup-cwd-traversal-rejected', False)
    except SetupConfigError:
        check('setup-cwd-traversal-rejected', True)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O06a local setup execution only; no host API calls',
}, ensure_ascii=False, indent=2))
