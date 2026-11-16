"""来源映射的真实临时 Git/桥接账本验证；宿主结果均为显式 fixture。"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from desktop_bridge import BridgePending, digest
from desktop_lifecycle_bridge import DesktopLifecycleBridgeAdapter, complete_tool_result
from resource_provenance import map_resource_provenance, normalize_artifacts

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


with tempfile.TemporaryDirectory(prefix='threadweave-provenance-') as td:
    base = Path(td).resolve()
    repo = base / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Validation')
    git(repo, 'config', 'user.email', 'validation@example.invalid')
    (repo / '.gitignore').write_text('.threadweave/\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'baseline')
    worktree = base / 'worker'
    git(repo, 'worktree', 'add', '-b', 'codex/provenance', str(worktree))
    run = 'provenance-fixture'

    def adapter(op, revision=2, source=None):
        return DesktopLifecycleBridgeAdapter(repo, run, op, 'a', 1, revision,
                    worktree_config={'sourceRoot': str(source or repo), 'ref': 'main'})

    def complete(op, method, result, revision=2, source=None):
        item = adapter(op, revision, source)
        try:
            method(item)
        except BridgePending:
            pass
        else:
            raise AssertionError('fixture must first prepare ' + op)
        item.journal.claim(op)
        raw = {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': False}
        path = base / (op + '-raw.json')
        path.write_text(json.dumps(raw))
        complete_tool_result(item.journal, op, raw)
        return path

    created = dict(type='created', worktreeGitRoot=str(worktree),
                   worktreeWorkspaceRoot=str(worktree), registrationError=None)
    creation_path = complete('create', lambda a: a.create_worktree('fixture-worker'), created, revision=0)
    artifacts = {'artifacts': [{'attachmentType': 'worktree', 'identityKey': str(worktree),
                'payload': {'hostId': 'local', 'root': str(worktree), 'workspaceRoot': str(worktree)}}]}
    inventory_path = complete('inventory', lambda a: a.list_artifacts(), artifacts)
    spec = dict(repository=str(repo), worktree=str(worktree), runId=run, role='a', attempt=1,
                createdTaskRevision=0, taskRevision=2, createOperation='create',
                createRaw=str(creation_path), inventoryOperation='inventory', inventoryRaw=str(inventory_path))

    def snapshot():
        return {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*.json')}

    def blocked(name, altered, **kwargs):
        before = snapshot()
        result = map_resource_provenance(altered, **kwargs)
        check(name, result['status'] == 'blocked' and result['cleanupEligible'] is False
              and result['deletionPerformed'] is False and snapshot() == before and worktree.exists())

    before = snapshot()
    result = map_resource_provenance(spec)
    check('association-without-host-run-id', result['status'] == 'associated'
          and result['runAssociationVerified'] and result['binding']['runId'] == run)
    check('revision-advancement-supported', result['binding']['taskRevision'] == 2)
    check('no-ownership-or-cleanup-inference', not result['cleanupEligible']
          and result['unknown'] == ['ownerThreadId', 'isPrimary', 'isShared', 'callingThreadId'])
    check('mapping-read-only', snapshot() == before and not git(worktree, 'status', '--porcelain'))
    for key, value in [('runId', 'other'), ('role', 'b'), ('attempt', 2),
                       ('taskRevision', 3), ('createdTaskRevision', 1), ('createOperation', 'inventory'),
                       ('worktree', str(base / 'other')), ('createRaw', str(base / 'missing'))]:
        blocked('wrong-' + key, {**spec, key: value})
    blocked('boolean-revision', {**spec, 'taskRevision': True})
    blocked('reversed-revisions', {**spec, 'createdTaskRevision': 3})
    stamp = datetime.fromisoformat(adapter('inventory').journal.inspect('inventory')['response']['observedAt'])
    blocked('stale-inventory', spec, now=stamp + timedelta(seconds=31))
    blocked('future-inventory', spec, now=stamp - timedelta(seconds=1))

    original = creation_path.read_bytes()
    creation_path.write_text(json.dumps({'content': [{'type': 'text', 'text': json.dumps({**created, 'type': 'queued'})}]}))
    blocked('raw-hash-drift', spec)
    creation_path.write_bytes(original)
    symlink = base / 'raw-alias.json'
    symlink.symlink_to(creation_path)
    blocked('raw-symlink', {**spec, 'createRaw': str(symlink)})

    journal_path = adapter('create').journal._path('create')
    original = journal_path.read_bytes()
    altered = json.loads(original)
    altered['request']['params']['ref'] = 'other'
    journal_path.write_text(json.dumps(altered))
    blocked('request-hash-drift', spec)
    journal_path.write_bytes(original)
    # 即使响应 hash 被重算，也必须与原始结果的解析内容相等。
    altered = json.loads(original)
    altered['response']['metadata']['result']['worktreeGitRoot'] = '/different'
    altered['responseSha'] = digest(altered['response'])
    journal_path.write_text(json.dumps(altered))
    blocked('metadata-raw-disagreement', spec)
    journal_path.write_bytes(original)

    queued_path = complete('create-queued', lambda a: a.create_worktree('fixture-pending'),
                            {'type': 'preparing', 'operationId': 'fixture-pending'}, revision=0)
    blocked('async-not-guessed-complete', {**spec, 'createOperation': 'create-queued', 'createRaw': str(queued_path)})
    # 补建新的创建回执后，旧清单不能证明它已经附加。
    later_path = complete('create-later', lambda a: a.create_worktree('fixture-later'), created, revision=0)
    blocked('inventory-before-creation', {**spec, 'createOperation': 'create-later', 'createRaw': str(later_path)})

    for name, mutate in [
        ('wrong-payload-root', lambda v: v['artifacts'][0]['payload'].update(root='/other')),
        ('wrong-payload-host', lambda v: v['artifacts'][0]['payload'].update(hostId='remote')),
        ('conflicting-types', lambda v: v['artifacts'][0].update(type='archived_worktree')),
        ('duplicate-identity', lambda v: v['artifacts'].append(copy.deepcopy(v['artifacts'][0]))),
        ('empty-inventory', lambda v: v.update(artifacts=[])),
    ]:
        value = copy.deepcopy(artifacts)
        mutate(value)
        path = complete(name, lambda a: a.list_artifacts(), value)
        blocked(name, {**spec, 'inventoryOperation': name, 'inventoryRaw': str(path)})

    archived = copy.deepcopy(artifacts)
    archived['artifacts'][0]['attachmentType'] = 'archived_worktree'
    archived['artifacts'][0]['payload'] = {'worktree': artifacts['artifacts'][0]['payload'], 'pullRequests': []}
    archived_path = complete('archived', lambda a: a.list_artifacts(), archived)
    result = map_resource_provenance({**spec, 'inventoryOperation': 'archived', 'inventoryRaw': str(archived_path)})
    check('archived-payload-normalized-without-deletion-claim', result['status'] == 'associated_archived'
          and not result['currentAttachmentObserved'] and not result['deletionPerformed'])
    legacy = copy.deepcopy(artifacts)
    legacy['artifacts'][0]['type'] = legacy['artifacts'][0].pop('attachmentType')
    check('legacy-fixture-type-supported', normalize_artifacts(legacy)[0]['kind'] == 'worktree')
    check('normalization-does-not-change-input', 'kind' not in legacy['artifacts'][0])

    manifest = base / 'manifest.json'
    manifest.write_text(json.dumps(spec))
    before = snapshot()
    cli = subprocess.run([sys.executable, str(project / 'scripts/resource_provenance.py'), str(manifest)],
                         text=True, capture_output=True, timeout=30)
    check('fresh-process-associates-without-execution', cli.returncode == 0
          and json.loads(cli.stdout)['status'] == 'associated' and snapshot() == before)
    manifest.write_text(json.dumps({**spec, 'role': 'wrong'}))
    cli = subprocess.run([sys.executable, str(project / 'scripts/resource_provenance.py'), str(manifest)],
                         text=True, capture_output=True, timeout=30)
    check('cli-blocked-is-nonzero', cli.returncode == 1 and json.loads(cli.stdout)['status'] == 'blocked')

print(json.dumps({'scope': 'controlled_integration', 'realHostValidated': False,
                  'passed': len(checks), 'checks': checks}, ensure_ascii=False))
