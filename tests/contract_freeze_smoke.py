"""验证 O08 稳定契约冻结、漂移阻断和批准后的证据失效。"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from contracts import ContractError, check_contracts, hash_contracts, load_stable_contracts  # noqa: E402
from controller import Controller  # noqa: E402
from host_adapter import UnsupportedHostAdapter  # noqa: E402
from report import report_hash  # noqa: E402
from state_store import read_state, run_state_path  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


with tempfile.TemporaryDirectory(prefix='threadweave-contract-') as d:
    root = Path(d) / 'repo'; root.mkdir()
    wt = Path(d) / 'worker'
    (root / 'api').mkdir()
    (root / 'src').mkdir()
    (root / 'api' / 'schema.json').write_text('{"version":1}\n', encoding='utf-8')
    (root / 'src' / 'a.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'threadweave.config.json').write_text(json.dumps({
        'stableContracts': ['api/schema.json']
    }), encoding='utf-8')
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    git(root, 'worktree', 'add', '-b', 'tw/a', str(wt), base)

    frozen = hash_contracts(wt, ['api/schema.json'])
    check('contract-hash-stable', len(frozen['contractHash']) == 64)
    check('contract-check-clean', check_contracts(wt, frozen)['changed'] == [])
    (wt / 'api' / 'schema.json').write_text('{"version":2}\n', encoding='utf-8')
    drift = check_contracts(wt, frozen)
    check('contract-drift-detected', drift['changed'] == ['api/schema.json'])
    git(wt, 'add', 'api/schema.json')
    git(wt, 'commit', '-m', 'change contract')
    head = git(wt, 'rev-parse', 'HEAD')
    git(wt, 'reset', '--hard', base)

    state = json.loads((project / 'references/state.example.json').read_text())
    state.update(runId='run-contract', status='observing', revision=0)
    state['project'].update(canonicalRepositoryRoot=str(root),
                            commonDirectory=str((root / '.git').resolve()))
    state['integration'].update(baseSha=base, branch='main', repoPath=str(root))
    state['workers'] = [{
        'role': 'a', 'attempt': 1, 'state': 'ready', 'threadId': 't-a',
        'branch': 'tw/a', 'baseSha': base, 'worktree': str(wt),
        'createdAt': '2026-11-16T00:00:00+08:00',
        'deadline': '2026-11-16T00:10:00+08:00',
        'lastObservation': '2026-11-16T00:00:00+08:00', 'errors': [],
        'ownedPaths': ['api/schema.json', 'src/a.py'], 'scopeExceptions': [],
        'activityRevision': 0,
    }]
    state['resources'] = []
    state['mergeOrder'] = []
    state['integrationLog'] = []
    state['finalReview'] = {'status': 'pending', 'notes': ''}
    state['cleanup'] = {'status': 'pending', 'preservedReports': [],
                        'outstandingResources': [], 'notes': ''}
    state['errors'] = []
    controller = Controller(UnsupportedHostAdapter(), root, 'run-contract', state)
    controller.save()
    freeze = controller.freeze_worker_contracts('a', 1)
    check('controlled-freeze', freeze['hashes']['api/schema.json'] == frozen['hashes']['api/schema.json'])
    (wt / 'api' / 'schema.json').write_text('{"version":2}\n', encoding='utf-8')
    git(wt, 'add', 'api/schema.json')
    git(wt, 'commit', '-m', 'change contract')
    head = git(wt, 'rev-parse', 'HEAD')

    worker = controller.state['workers'][0]
    worker.update(state='running', activityRevision=1)
    report = {
        'runId': 'run-contract', 'role': 'a', 'attempt': 1,
        'threadId': 't-a', 'branch': 'tw/a', 'head': head,
        'commitSha': head, 'taskRevision': 1, 'baseIsAncestor': True,
        'filesChanged': ['api/schema.json'], 'testsRun': [],
        'selfReportedVerification': [], 'contractChanges': [],
        'knownRisks': [],
    }
    report['reportSha'] = report_hash(report)
    before = copy.deepcopy(controller.state)
    try:
        controller.accept_report('a', 1, report)
        check('contract-drift-blocks-report', False)
    except RuntimeError as exc:
        check('contract-drift-blocks-report', 'contract_drift' in str(exc))
    check('contract-drift-state-unchanged', controller.state == before)

    approved = controller.approve_contract_changes('a', 1, 'leader approved API schema v2')
    check('approved-history-recorded', approved['history']['changedPaths'] == ['api/schema.json'])
    worker = controller.state['workers'][0]
    check('approval-invalidates-old-report', 'report' not in worker)
    check('approval-advances-revision', worker['activityRevision'] == 2)
    report2 = dict(report, taskRevision=2, reportSha=None)
    report2['reportSha'] = report_hash({k: v for k, v in report2.items() if k not in ('reportSha',)})
    controller.accept_report('a', 1, report2)
    saved = read_state(run_state_path(root, 'run-contract'))
    check('approved-report-accepted', saved['workers'][0]['state'] == 'reported')
    check('approval-history-persisted', len(saved['workers'][0]['contractHistory']) == 1)

    try:
        load_stable_contracts(root / 'missing')
    except ContractError:
        check('missing-config-not-converted-to-contract', False)
    else:
        check('missing-config-not-converted-to-contract', True)
    bad_config = root / 'threadweave.config.json'
    original = bad_config.read_text()
    bad_config.write_text(json.dumps({'stableContracts': ['../outside.json']}))
    try:
        load_stable_contracts(root)
        check('contract-path-traversal-rejected', False)
    except ContractError:
        check('contract-path-traversal-rejected', True)
    bad_config.write_text(original)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O08 local contract freeze only; no host API calls',
}, ensure_ascii=False, indent=2))
