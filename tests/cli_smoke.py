"""验证本地辅助 CLI 的 preflight、status 与 cleanup-plan，不调用宿主 API。"""
import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from state_store import run_state_path, write_state_atomic  # noqa: E402
from threadweave import cleanup_plan, doctor, inspect, main, preflight, status  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def git(root, *args):
    r = subprocess.run(
        ['git', '-c', 'user.name=Threadweave Test',
         '-c', 'user.email=threadweave-test@example.invalid', *args],
        cwd=root, text=True, capture_output=True,
    )
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r


with tempfile.TemporaryDirectory(prefix='threadweave-cli-') as d:
    d = Path(d)
    repo = d / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    (repo / 'a.txt').write_text('a\n')
    git(repo, 'add', 'a.txt')
    git(repo, 'commit', '-m', 'init')

    checks = preflight(repo)
    check('preflight-inside', checks['inside_work_tree'])
    check('preflight-has-head', checks['has_head'])
    check('preflight-attached', checks['attached_branch'])
    check('preflight-clean', checks['clean'])

    (repo / 'dirty.txt').write_text('dirty\n')
    check('preflight-dirty', not preflight(repo)['clean'])
    (repo / 'dirty.txt').unlink()

    git(repo, 'checkout', '--detach')
    check('preflight-detached', not preflight(repo)['attached_branch'])
    git(repo, 'checkout', 'main')

    write_state_atomic(run_state_path(repo, 'run-1'), {
        'schemaVersion': '1.0', 'runId': 'run-1', 'status': 'observing',
    })
    st = status(repo)
    check('status-finds-run', any(r['runId'] == 'run-1' for r in st['runs']))
    check('status-latest', st['latestIncomplete']['runId'] == 'run-1')

    obs = d / 'obs.json'
    obs.write_text(json.dumps({
        'a#1': {
            'worktree': {
                'worktree_exists': True, 'status_porcelain': '',
                'unmerged_commits': False, 'worker_status': 'idle',
                'owned_by_run': True, 'is_primary': False,
            },
            'report_archived': True,
            'branch': {'branch_exists': True, 'merged_into_integration': True},
            'stop': {'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True},
        }
    }), encoding='utf-8')
    write_state_atomic(run_state_path(repo, 'run-2'), {
        'schemaVersion': '1.0', 'runId': 'run-2', 'status': 'final-review',
        'workers': [{'role': 'a', 'attempt': 1}],
    })
    plan = cleanup_plan(repo, 'run-2', obs)
    check('cleanup-plan-actions',
          plan['a#1'] == {'worktree': 'remove', 'branch': 'delete', 'stop_evidence': 'stopped'})

    bad_repo = d / 'not-git'
    bad_repo.mkdir()
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = main(['preflight', '--repo', str(bad_repo)])
    check('preflight-failure-exit-code', exit_code == 1)
    check('doctor-ok-empty', doctor(bad_repo)['ok'])
    check('inspect-empty', inspect(bad_repo)['runs'] == [])

    unreadable = repo / '.threadweave' / 'run-array' / 'state.json'
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    unreadable.write_text('[]', encoding='utf-8')
    check('doctor-detects-unreadable', not doctor(repo)['ok'])
    check('inspect-marks-unreadable',
          any(run['status'] == 'unreadable' for run in inspect(repo)['runs']))

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Local helper CLI only; no host API calls',
}, ensure_ascii=False, indent=2))
