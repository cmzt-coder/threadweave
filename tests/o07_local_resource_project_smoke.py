"""O07 本地真实 Git 资源项目 fixture 回归。"""
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller  # noqa: E402
from host_adapter import HostAdapter  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


class FakeAdapter(HostAdapter):
    def __init__(self):
        self.calls = []

    def create_worker(self, prompt, project_id):
        self.calls.append(('create_worker', prompt, project_id))
        return {'threadId': 't'}

    def send_message(self, thread_id, message):
        self.calls.append(('send_message', thread_id, message))

    def read_thread(self, thread_id):
        return {'status': 'idle'}

    def wait_threads(self, thread_ids, timeout_seconds):
        return {'timedOut': False}

    def create_worktree(self, name):
        return {'root': name}

    def archive_worktree(self, root):
        return None


def make_state(base_state, root, run_id, workers, base):
    state = json.loads(json.dumps(base_state))
    state.update(runId=run_id, status='dispatching', revision=0,
                 createdAt='2026-11-16T00:00:00+08:00',
                 updatedAt='2026-11-16T00:00:00+08:00')
    state['project'].update(
        canonicalRepositoryRoot=str(root), commonDirectory=str((root / '.git').resolve()))
    state['integration'] = {'baseSha': base, 'branch': 'main', 'repoPath': str(root)}
    state['authorization'] = {'provenance': 'local O07 fixture', 'messagingScope': 'local fake adapter'}
    state['limits'] = {'workers': 2, 'initializationTimeoutSeconds': 300,
                       'replacementPerRole': 1, 'maxReworkPerWorker': 2}
    state['workers'] = workers
    state['resources'] = []
    state['mergeOrder'] = []
    state['integrationLog'] = []
    state['finalReview'] = {'status': 'pending', 'notes': ''}
    state['cleanup'] = {'status': 'pending', 'preservedReports': [],
                        'outstandingResources': [], 'notes': ''}
    state['errors'] = []
    return state


def worker(role, branch, path, base):
    return {
        'role': role, 'attempt': 1, 'state': 'ready', 'threadId': 't-' + role,
        'branch': branch, 'baseSha': base, 'worktree': str(path),
        'createdAt': '2026-11-16T00:00:00+08:00',
        'deadline': '2026-11-16T00:10:00+08:00',
        'lastObservation': '2026-11-16T00:00:00+08:00', 'errors': [],
        'ownedPaths': [f'migrations/{role}.sql'], 'scopeExceptions': [],
        'activityRevision': 0,
    }


def startup(role, branch, path, root, base):
    return {
        'role': role, 'attempt': 1,
        'observedAt': datetime.now(timezone.utc).isoformat(),
        'cwd': str(path), 'worktree_root': str(path),
        'git_common_dir': str((root / '.git').resolve()),
        'branch': branch, 'head': base, 'status_porcelain': '',
    }


with tempfile.TemporaryDirectory(prefix='threadweave-o07-') as td:
    root = Path(td) / 'repo'
    root.mkdir()
    (root / 'migrations').mkdir()
    (root / 'scripts').mkdir()
    (root / 'migrations' / '001_init.sql').write_text('CREATE TABLE init (id INTEGER);\n')
    (root / 'migrations' / 'a.sql').write_text('ALTER TABLE init ADD COLUMN a INTEGER;\n')
    (root / 'migrations' / 'b.sql').write_text('ALTER TABLE init ADD COLUMN b INTEGER;\n')
    (root / 'threadweave.config.json').write_text(json.dumps({
        'serializedResources': [{'name': 'migration-order', 'paths': ['migrations/**']}]
    }))
    (root / 'scripts' / 'generate_manifest.py').write_text(
        "import hashlib,json\nfrom pathlib import Path\n"
        "files=sorted((Path(__file__).parents[1]/'migrations').glob('*.sql'))\n"
        "print(json.dumps([p.name for p in files]))\n")
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    paths = {}
    for name in ('a', 'b', 'single'):
        worktree = Path(td) / name
        git(root, 'worktree', 'add', '-b', f'o07/{name}', str(worktree), base)
        paths[name] = worktree
    (paths['a'] / 'migrations' / 'a.sql').write_text('ALTER TABLE init ADD COLUMN a INTEGER;\nCREATE INDEX ia ON init(a);\n')
    (paths['b'] / 'migrations' / 'b.sql').write_text('ALTER TABLE init ADD COLUMN b INTEGER;\nCREATE INDEX ib ON init(b);\n')
    for name in ('a', 'b'):
        git(paths[name], 'add', '.')
        git(paths[name], 'commit', '-m', name)
    (paths['single'] / 'migrations' / 'a.sql').write_text('ALTER TABLE init ADD COLUMN a INTEGER;\nCREATE INDEX ia ON init(a);\n')
    (paths['single'] / 'migrations' / 'b.sql').write_text('ALTER TABLE init ADD COLUMN b INTEGER;\nCREATE INDEX ib ON init(b);\n')
    git(paths['single'], 'add', '.')
    git(paths['single'], 'commit', '-m', 'single owner')

    base_state = json.loads((project / 'references/state.example.json').read_text())
    conflict_adapter = FakeAdapter()
    conflict_state = make_state(base_state, root, 'o07-conflict', [
        dict(worker('a', 'o07/a', paths['a'], base), ownedPaths=['migrations/a.sql']),
        dict(worker('b', 'o07/b', paths['b'], base), ownedPaths=['migrations/b.sql']),
    ], base)
    conflict = Controller(conflict_adapter, root, 'o07-conflict', conflict_state)
    expected_a = {
        'branch': 'o07/a', 'base': base,
        'common_dir': str((root / '.git').resolve()),
        'worker_root': str(paths['a']),
        'other_roots': [str(root), str(paths['b'])],
        'integration_head': base,
    }
    try:
        conflict.dispatch('a', 1, 'conflict', startup('a', 'o07/a', paths['a'], root, base), expected_a)
        check('resource-conflict-raised', False)
    except RuntimeError as exc:
        check('resource-conflict-raised', 'scope reservation conflict' in str(exc))
    check('conflict-no-send', conflict_adapter.calls == [])

    single_adapter = FakeAdapter()
    single_state = make_state(base_state, root, 'o07-single', [
        dict(worker('single', 'o07/single', paths['single'], base),
             ownedPaths=['migrations/a.sql', 'migrations/b.sql']),
    ], base)
    single = Controller(single_adapter, root, 'o07-single', single_state)
    expected_single = {
        'branch': 'o07/single', 'base': base,
        'common_dir': str((root / '.git').resolve()),
        'worker_root': str(paths['single']),
        'other_roots': [str(root)], 'integration_head': base,
    }
    single.dispatch('single', 1, 'single owner', startup('single', 'o07/single', paths['single'], root, base), expected_single)
    check('single-owner-send', single_adapter.calls and single_adapter.calls[0][0] == 'send_message')

    git(root, 'merge', '--ff-only', 'o07/single')
    generated = subprocess.check_output(
        [sys.executable, str(root / 'scripts' / 'generate_manifest.py')], text=True)
    check('post-integration-generation', json.loads(generated) == ['001_init.sql', 'a.sql', 'b.sql'])
    check('integration-head', git(root, 'rev-parse', 'HEAD') == git(root, 'rev-parse', 'o07/single'))

    for name in ('a', 'b', 'single'):
        git(root, 'worktree', 'remove', str(paths[name]))

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O07 local real Git resource fixture; no host API calls',
}, ensure_ascii=False, indent=2))
