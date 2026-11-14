"""验证 O11 失联证据、旧成果保全和 replacement 授权。"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller  # noqa: E402
from host_adapter import UnsupportedHostAdapter  # noqa: E402
from recovery import build_preservation_record, build_recovery_plan  # noqa: E402
from state_store import read_state, run_state_path  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def rejected(name, fn):
    try:
        fn()
    except ValueError:
        check(name, True)
    else:
        check(name, False)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], text=True).strip()


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'base.txt').write_text('base', encoding='utf-8')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    git(root, 'branch', 'tw/a')

    state = json.loads((project / 'references/state.example.json').read_text())
    state.update(runId='lost-run', status='observing', revision=0)
    state['status'] = 'observing'
    state['project'].update(
        canonicalRepositoryRoot=str(root), commonDirectory=str((root / '.git').resolve()))
    state['integration'].update(
        baseSha=base, branch='main', repoPath=str(root), currentSha=base)
    state['workers'] = [{
        'role': 'a', 'attempt': 1, 'state': 'running', 'threadId': 't-a',
        'branch': 'tw/a', 'baseSha': base,
        'worktree': str(Path(td) / 'missing-worker'),
        'createdAt': '2026-11-14T00:00:00+08:00',
        'deadline': '2026-11-14T00:10:00+08:00',
        'lastObservation': '2026-11-14T00:00:00+08:00',
        'errors': [],
    }]
    state['resources'] = []
    state['mergeOrder'] = []
    state['integrationLog'] = []
    state['finalReview'] = {'status': 'pending', 'notes': ''}
    state['cleanup'] = {
        'status': 'pending', 'preservedReports': [],
        'outstandingResources': [], 'notes': '',
    }
    state['errors'] = []
    controller = Controller(UnsupportedHostAdapter(), root, 'lost-run', state)
    controller.save()

    observed = {
        'thread_exists': False,
        'thread_status': None,
        'thread_absence_confirmed': True,
        'thread_absence_evidence': 'list_threads exact lookup returned not found twice',
        'worktree_exists': False,
        'branch': 'tw/a',
        'head': base,
        'branch_preserved': True,
        'report_present': False,
        'notes_present': False,
    }
    before_plan = build_recovery_plan(
        read_state(run_state_path(root, 'lost-run')), {'a#1': observed})
    check('confirmation-before-preservation', before_plan['a#1']['action'] == 'preserve_then_recreate')

    try:
        controller.authorize_recovery_replacement('a', 1)
        check('replacement-requires-preservation', False)
    except RuntimeError:
        check('replacement-requires-preservation', True)

    record = controller.record_recovery_preservation('a', 1, observed)
    check('preservation-recorded', len(record['preservationSha']) == 64)
    persisted = read_state(run_state_path(root, 'lost-run'))
    check('preservation-persisted',
          persisted['workers'][0]['recoveryPreservation']['head'] == base)

    authorization = controller.authorize_recovery_replacement('a', 1)
    check('replacement-authorized', authorization['nextAttempt'] == 2)
    persisted = read_state(run_state_path(root, 'lost-run'))
    check('authorization-persisted',
          persisted['workers'][0]['recoveryReplacementAuthorization']['preservationSha'] == record['preservationSha'])

    after_plan = build_recovery_plan(persisted, {'a#1': observed})
    check('preservation-enables-recreate', after_plan['a#1']['action'] == 'recreate')
    check('preservation-gaps-empty', after_plan['a#1']['preservationGaps'] == [])

    dirty = dict(observed, worktree_exists=True, status_porcelain=' M base.txt',
                 unmerged_commits=False, dirty_patch_archived=False,
                 report_present=False, notes_present=False)
    rejected('dirty-worktree-needs-patch', lambda: build_preservation_record(
        persisted['workers'][0], dirty))
    unconfirmed = dict(observed, thread_absence_confirmed=False)
    rejected('unconfirmed-thread-needs-recheck', lambda: build_preservation_record(
        persisted['workers'][0], unconfirmed))

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O11 local preservation and replacement authorization; no host API calls',
}, ensure_ascii=False, indent=2))
