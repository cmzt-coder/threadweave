"""验证 O13 分层 preflight 契约、局部失效和旧格式标记。"""
import copy
import json
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller  # noqa: E402
from host_adapter import UnsupportedHostAdapter  # noqa: E402
from preflight_contract import build_preflight_contract, evaluate_preflight  # noqa: E402
from startup_gate import validate_startup  # noqa: E402
from state_schema import validate_state  # noqa: E402
from state_store import read_state, run_state_path  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


run_inputs = {
    'projectId': 'p1', 'hostId': 'local', 'repoRoot': '/repo',
    'baseSha': 'a' * 40, 'integrationBranch': 'main',
}
workers = {
    'a#1': {'baseSha': 'a' * 40, 'branch': 'tw/a', 'ownedPaths': ['src/a/**']},
    'b#1': {'baseSha': 'a' * 40, 'branch': 'tw/b', 'ownedPaths': ['src/b/**']},
}
contract = build_preflight_contract(
    'run-pf', run_inputs, workers, created_at='2026-11-16T00:00:00+08:00')
same = build_preflight_contract(
    'run-pf', copy.deepcopy(run_inputs), copy.deepcopy(workers),
    created_at='2026-11-16T01:00:00+08:00')
check('contract-hash-stable', contract['contractHash'] == same['contractHash'])
check('preflight-id-format', contract['preflightId'].startswith('pf_'))

valid = evaluate_preflight(contract, run_inputs, workers)
check('contract-valid', valid['status'] == 'valid' and valid['runValid'])
check('workers-valid', all(v['valid'] for v in valid['workers'].values()))

run_changed = dict(run_inputs, baseSha='b' * 40)
invalid_run = evaluate_preflight(contract, run_changed, workers)
check('run-change-invalidates', invalid_run['status'] == 'invalidated' and invalid_run['runChanged'])
check('run-change-global', all(not v['valid'] for v in invalid_run['workers'].values()))

worker_changed = copy.deepcopy(workers)
worker_changed['a#1']['ownedPaths'] = ['src/a/**', 'src/shared/**']
invalid_worker = evaluate_preflight(contract, run_inputs, worker_changed)
check('worker-change-local-a', not invalid_worker['workers']['a#1']['valid'])
check('worker-change-local-b', invalid_worker['workers']['b#1']['valid'])

missing_worker = {'a#1': workers['a#1']}
missing = evaluate_preflight(contract, run_inputs, missing_worker)
check('missing-worker-invalid', not missing['workers']['b#1']['valid'])
new_worker = dict(workers, **{'c#1': {'baseSha': 'a' * 40, 'branch': 'tw/c'}})
added = evaluate_preflight(contract, run_inputs, new_worker)
check('unregistered-worker-invalid', not added['workers']['c#1']['valid'])

fresh = evaluate_preflight(
    contract, run_inputs, workers,
    run_evidence_at='2026-11-16T12:00:00+08:00',
    worker_evidence_at={'a#1': '2026-11-16T12:00:00+08:00',
                        'b#1': '2026-11-16T12:00:00+08:00'},
    max_age_seconds=60, now='2026-11-16T12:00:30+08:00')
check('fresh-evidence-valid', fresh['status'] == 'valid')
stale = evaluate_preflight(
    contract, run_inputs, workers,
    run_evidence_at='2026-11-16T10:00:00+08:00',
    worker_evidence_at={'a#1': '2026-11-16T10:00:00+08:00',
                        'b#1': '2026-11-16T10:00:00+08:00'},
    max_age_seconds=60, now='2026-11-16T12:00:00+08:00')
check('stale-evidence-invalid', stale['status'] == 'invalidated')
check('stale-worker-reason', 'evidence_stale' in stale['workers']['a#1']['reasons'])
missing_evidence = evaluate_preflight(
    contract, run_inputs, workers, max_age_seconds=60,
    now='2026-11-16T12:00:00+08:00')
check('missing-evidence-invalid', missing_evidence['status'] == 'invalidated')
legacy = evaluate_preflight({'legacy': True}, run_inputs, workers)
check('legacy-marked-unverified', legacy['status'] == 'unsupported_legacy' and not legacy['valid'])

state = json.loads((project / 'references/state.example.json').read_text())
state['runId'] = 'run-pf'
state['preflight'] = contract
validate_state(state)
check('contract-schema-compatible', True)

startup = {
    'cwd': '/repo/worktree', 'worktree_root': '/repo/worktree',
    'git_common_dir': '/repo/.git', 'branch': 'tw/a', 'head': 'a' * 40,
    'status_porcelain': '', 'preflight_id': contract['preflightId'],
}
expected = {
    'common_dir': '/repo/.git', 'branch': 'tw/a', 'base': 'a' * 40,
    'worker_root': '/repo/worktree', 'other_roots': ['/leader'],
    'preflight_id': contract['preflightId'],
}
check('startup-preflight-match', validate_startup(startup, expected) == (True, []))
startup_bad = dict(startup, preflight_id='pf_0000000000000000')
ok, reasons = validate_startup(startup_bad, expected)
check('startup-preflight-mismatch', not ok and 'preflight_id_mismatch' in reasons)

with tempfile.TemporaryDirectory() as d:
    state2 = json.loads((project / 'references/state.example.json').read_text())
    state2['runId'] = 'run-controller-pf'
    controller = Controller(UnsupportedHostAdapter(), d, 'run-controller-pf', state2)
    saved_contract = controller.record_preflight_contract(run_inputs, workers)
    check('controller-contract-persisted',
          read_state(run_state_path(d, 'run-controller-pf'))['preflight']['contractHash']
          == saved_contract['contractHash'])
    check('controller-evaluate', controller.evaluate_preflight_contract(
        run_inputs, workers)['status'] == 'valid')

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O13 local preflight contract and invalidation only',
}, ensure_ascii=False, indent=2))
