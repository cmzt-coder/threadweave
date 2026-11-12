"""验证 durable 状态位置与恢复定位，不驱动真实 Threadweave 流程。"""
import json
import sys
import tempfile
import time
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from state_store import (  # noqa: E402
    latest_incomplete_run, locate_runs, read_state, run_state_dir,
    run_state_path, write_state_atomic,
)

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


with tempfile.TemporaryDirectory(prefix='threadweave-recovery-') as d:
    d = Path(d)
    repo = d / 'leader'
    worker = d / 'worker'
    repo.mkdir()
    worker.mkdir()

    r1_dir = run_state_dir(repo, 'run-1')
    r2_dir = run_state_dir(repo, 'run-2')
    check('run-dir-under-repo', str(r1_dir).startswith(str(repo / '.threadweave')))
    check('run-dir-distinct', r1_dir != r2_dir)
    check('state-file-name', run_state_path(repo, 'run-1').name == 'state.json')
    check('state-outside-worker', not str(r1_dir).startswith(str(worker)))

    write_state_atomic(run_state_path(repo, 'run-1'), {
        'schemaVersion': '1.0', 'runId': 'run-1', 'status': 'observing',
        'workers': [{'role': 'a', 'attempt': 1}],
    })
    time.sleep(0.01)
    write_state_atomic(run_state_path(repo, 'run-2'), {
        'schemaVersion': '1.0', 'runId': 'run-2', 'status': 'complete',
        'workers': [],
    })
    runs = locate_runs(repo)
    check('locate-two-runs', len(runs) == 2)
    check('locate-run-ids', {r['runId'] for r in runs} == {'run-1', 'run-2'})

    latest = latest_incomplete_run(repo)
    check('latest-incomplete-skips-complete',
          latest is not None and latest['runId'] == 'run-1')

    broken = run_state_dir(repo, 'run-broken') / 'state.json'
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text('{not json', encoding='utf-8')
    runs = locate_runs(repo)
    check('broken-state-marked',
          any(r['runId'] == 'run-broken' and r['status'] == 'unreadable' for r in runs))
    latest = latest_incomplete_run(repo)
    check('broken-state-not-latest',
          latest is not None and latest['runId'] == 'run-1')

    recovered = read_state(run_state_path(repo, 'run-1'))
    check('recover-run-id', recovered['runId'] == 'run-1')
    check('recover-status', recovered['status'] == 'observing')
    check('recover-workers-preserved', recovered['workers'][0]['role'] == 'a')

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Durable state location and recovery scan only; no real runtime',
}, ensure_ascii=False, indent=2))
