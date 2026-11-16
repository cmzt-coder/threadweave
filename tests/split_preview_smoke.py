"""验证 O10 只读拆分预演，不派发、不评分、不修改仓库。"""
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from split_preview import build_split_preview  # noqa: E402
from threadweave import main  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def worker(role, owned, read=None, write=None):
    return {
        'role': role, 'attempt': 1, 'ownedPaths': owned,
        'scopeExceptions': [], 'readContracts': read or [], 'writeContracts': write or [],
    }


disjoint = build_split_preview(
    [worker('a', ['src/a/**']), worker('b', ['src/b/**'])], repo_root=project)
check('disjoint-no-known-conflict', disjoint['status'] == 'no-known-conflict')
check('disjoint-no-auto-dispatch', disjoint['autoDispatch'] is False)
check('disjoint-readonly', disjoint['readOnly'] is True)
check('disjoint-not-safety-score', 'score' not in disjoint)

overlap = build_split_preview(
    [worker('a', ['src/**']), worker('b', ['src/future.py'])], repo_root=project)
check('future-overlap-conflict', overlap['status'] == 'conflict')
check('future-overlap-reason', any('范围冲突' in x for x in overlap['writeSetConflicts']))

resource = build_split_preview(
    [worker('a', ['migrations/01.sql']), worker('b', ['migrations/02.sql'])],
    serialized_resources=[{'name': 'migration-order', 'paths': ['migrations/**']}],
    repo_root=project)
check('resource-group-conflict', resource['status'] == 'conflict')
check('resource-group-reason', bool(resource['resourceConflicts']))

contracts = build_split_preview(
    [worker('a', ['src/a.py'], write=['api/schema.json']),
     worker('b', ['src/b.py'], read=['api/schema.json'])],
    repo_root=project)
check('contract-dependency', contracts['status'] == 'no-known-conflict'
      and contracts['contractDependencies'])
check('contract-writer-reader',
      contracts['contractDependencies'][0]['writer'] == 'a#1'
      and contracts['contractDependencies'][0]['readers'] == ['b#1'])

multi_writer = build_split_preview(
    [worker('a', ['src/a.py'], write=['api/schema.json']),
     worker('b', ['src/b.py'], write=['api/schema.json'])],
    repo_root=project)
check('contract-multi-writer-conflict', multi_writer['status'] == 'conflict'
      and multi_writer['contractConflicts'])

invalid = build_split_preview(
    [worker('a', ['src/a.py'], read=['../secret'])], repo_root=project)
check('invalid-contract-needs-review', invalid['status'] == 'needs-review')
check('invalid-contract-unknown', invalid['unknowns'])

with tempfile.TemporaryDirectory() as d:
    path = Path(d) / 'preview.json'
    path.write_text(json.dumps({
        'workers': [worker('a', ['src/a.py']), worker('b', ['src/b.py'])],
        'serializedResources': [],
    }), encoding='utf-8')
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(['split-preview', '--repo', str(project), '--input', str(path)])
    cli = json.loads(output.getvalue())
    check('split-preview-cli', code == 0 and cli['status'] == 'no-known-conflict')

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O10 split preview only; no dispatch, score, merge or cleanup',
}, ensure_ascii=False, indent=2))
