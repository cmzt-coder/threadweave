"""O16 host archive 完成审计的真实临时 Git/账本 fixture；不执行真实宿主调用。"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from desktop_bridge import BridgePending, digest
from desktop_lifecycle_bridge import DesktopLifecycleBridgeAdapter, complete_tool_result
from archive_authorization import build_envelope
from cleanup_apply import preview
from host_archive_guard import PROTECTION_POLICY, verify_host_archive

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()


with tempfile.TemporaryDirectory(prefix='threadweave-archive-guard-') as td:
    base = Path(td).resolve()
    repo = base / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Validation')
    git(repo, 'config', 'user.email', 'validation@example.invalid')
    (repo / 'tracked').write_text('baseline')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'baseline')
    worktree = base / 'worker'
    git(repo, 'worktree', 'add', '-b', 'codex/archive-guard', str(worktree))
    run = 'archive-guard-fixture'
    revision = 2

    def adapter(operation, task_revision=revision, archive=False):
        return DesktopLifecycleBridgeAdapter(
            repo, run, operation, 'a', 1, task_revision,
            worktree_config={'sourceRoot': str(repo), 'ref': 'main'},
            archive_roots=(str(worktree),) if archive else ())

    def complete(operation, call, result, task_revision=revision, archive=False):
        item = adapter(operation, task_revision, archive)
        try:
            call(item)
        except BridgePending:
            pass
        else:
            raise AssertionError('fixture must first prepare ' + operation)
        item.journal.claim(operation)
        raw = {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': False}
        path = base / (operation + '-raw.json')
        path.write_text(json.dumps(raw))
        complete_tool_result(item.journal, operation, raw)
        return path, item.journal.inspect(operation)

    created = {
        'type': 'created',
        'worktreeGitRoot': str(worktree),
        'worktreeWorkspaceRoot': str(worktree),
        'registrationError': None,
    }
    create_path, create_record = complete(
        'create', lambda a: a.create_worktree('guard-worker'), created, task_revision=0)
    active = {
        'artifacts': [{
            'attachmentType': 'worktree',
            'identityKey': str(worktree),
            'payload': {'hostId': 'local', 'root': str(worktree),
                        'workspaceRoot': str(worktree), 'sourceCwd': str(repo)},
        }]
    }
    pre_path, _ = complete('pre-inventory', lambda a: a.list_artifacts(), active)
    archive_item = adapter('archive', archive=True)
    try:
        archive_item.archive_worktree(str(worktree))
    except BridgePending:
        pass
    else:
        raise AssertionError('archive fixture must first prepare')
    archive_item.journal.claim('archive')
    archive_record = archive_item.journal.inspect('archive')
    report = base / 'report.md'
    report.write_text('accepted')
    evidence_path = base / 'evidence.json'
    authorization_time = datetime.now(timezone.utc)
    evidence = {
        'observedAt': authorization_time.isoformat(),
        'runId': run,
        'worktree': str(worktree),
        'branch': 'codex/archive-guard',
        'head': git(worktree, 'rev-parse', 'HEAD'),
        'reportSha256': hashlib.sha256(report.read_bytes()).hexdigest(),
        'ownedByRun': True,
        'shared': False,
        'acceptanceVerified': True,
        'executionKind': 'host_thread',
        'threadId': 'thread-guard',
        'stop': {'threadIdle': True, 'finalAnswerPresent': True, 'noActiveTurn': True},
    }
    evidence_path.write_text(json.dumps(evidence))
    declaration = {
        'runId': run,
        'repository': str(repo),
        'worktree': str(worktree),
        'branch': 'codex/archive-guard',
        'head': evidence['head'],
        'integrationRef': 'main',
        'report': str(report),
        'reportSha256': evidence['reportSha256'],
        'evidence': str(evidence_path),
    }
    approved = preview(declaration)
    pre_record = adapter('pre-inventory').journal.inspect('pre-inventory')
    pre_receipt = {
        'runId': run, 'role': 'a', 'attempt': 1, 'taskRevision': revision,
        'source': pre_record['response']['source'], 'operationId': 'pre-inventory',
        'observedAt': pre_record['response']['observedAt'],
        'requestSha': pre_record['requestSha'],
        'toolResultSha': pre_record['response']['toolResultSha'],
        'metadata': pre_record['response']['metadata'],
    }
    stop_receipt = {
        'runId': run, 'role': 'a', 'attempt': 1, 'taskRevision': revision,
        'source': 'desktop_tool_bridge', 'operationId': 'wait-stop',
        'observedAt': authorization_time.isoformat(),
        'metadata': {'hostId': 'local', 'result': {'polls': [{
            'thread': {'id': 'thread-guard', 'hostId': 'local', 'status': {'type': 'idle'}},
            'latestTurn': {'id': 'turn-guard', 'status': 'completed'},
            'latestAssistantMessage': {'turnId': 'turn-guard', 'phase': 'final_answer'},
        }]}},
    }
    auth_manifest = {
        'declaration': declaration,
        'approvedPlan': approved,
        'role': 'a',
        'attempt': 1,
        'taskRevision': revision,
        'stopReceipt': stop_receipt,
        'preInventoryReceipt': pre_receipt,
        'archiveRequestSha': archive_record['requestSha'],
        'authorizationNonce': 'host-guard-authorization',
        'authorizedAt': authorization_time.isoformat(),
        'expiresAt': (authorization_time + timedelta(seconds=120)).isoformat(),
    }
    auth_result = build_envelope(auth_manifest, now=authorization_time)
    if auth_result['status'] != 'envelope_prepared':
        raise AssertionError('authorization envelope setup failed')
    archive_raw = {'content': [{'type': 'text', 'text': json.dumps({'type': 'queued'})}], 'isError': False}
    archive_path = base / 'archive-raw.json'
    archive_path.write_text(json.dumps(archive_raw))
    complete_tool_result(archive_item.journal, 'archive', archive_raw)
    archive_record = archive_item.journal.inspect('archive')
    git(repo, 'worktree', 'remove', str(worktree))
    archived = {
        'artifacts': [{
            'attachmentType': 'archived_worktree',
            'identityKey': str(worktree),
            'payload': {
                'worktree': {'hostId': 'local', 'root': str(worktree),
                             'workspaceRoot': str(worktree), 'sourceGitRoot': str(repo)},
                'pullRequests': [],
            },
        }]
    }
    completion_path, _ = complete(
        'completion-inventory', lambda a: a.list_artifacts(), archived)
    spec = {
        'protectionPolicy': PROTECTION_POLICY,
        'repository': str(repo),
        'worktree': str(worktree),
        'runId': run,
        'role': 'a',
        'attempt': 1,
        'createdTaskRevision': 0,
        'taskRevision': revision,
        'createOperation': 'create',
        'createRaw': str(create_path),
        'preInventoryOperation': 'pre-inventory',
        'preInventoryRaw': str(pre_path),
        'archiveOperation': 'archive',
        'archiveRaw': str(archive_path),
        'expectedArchiveRequestSha': archive_record['requestSha'],
        'completionInventoryOperation': 'completion-inventory',
        'completionInventoryRaw': str(completion_path),
        'requireAuthorization': True,
        'authorization': {'manifest': auth_manifest, 'result': auth_result},
    }

    result = verify_host_archive(spec)
    check('archive-completion-observed', result['status'] == 'host_archive_completion_observed'
          and result['completionEvidenceVerified'] is True and result['hostArchiveCompleted'] is True)
    check('completion-does-not-grant-cleanup', result['cleanupEligible'] is False
          and result['deletionPerformed'] is False
          and result['preObservableProtection'] is False)
    check('protection-delegation-explicit', result['protectionDelegation'] == PROTECTION_POLICY)
    check('protection-enforcement-not-claimed', result['protectionEnforcementVerified'] is False)
    check('caller-continuity-not-claimed', result['callerContinuityVerified'] is False)
    check('approval-not-claimed', result['approvalBindingVerified'] is False)
    check('pre-inventory-shape-verified', result['preInventoryShapeVerified'] is True)
    check('authorization-binding-verified', result['authorizationBindingVerified'] is True
          and result['authorizationFingerprint'] == auth_result['authorizationFingerprint'])
    check('host-identity-still-unverified', result['hostSignedIdentityVerified'] is False)
    check('unknown-ownership-preserved', result['unknown'] == [
        'ownerThreadId', 'isPrimary', 'isShared', 'callingThreadId'])

    def blocked(name, altered, **kwargs):
        value = verify_host_archive(altered, **kwargs)
        check(name, value['status'] == 'blocked' and value['cleanupEligible'] is False
              and value['deletionPerformed'] is False)

    blocked('wrong-protection-policy', {**spec, 'protectionPolicy': 'payload-fields'})
    blocked('missing-required-authorization', {**spec, 'authorization': None})
    bad_auth_result = copy.deepcopy(auth_result)
    bad_auth_result['authorizationFingerprint'] = '0' * 64
    blocked('tampered-authorization', {**spec, 'authorization': {
        'manifest': auth_manifest, 'result': bad_auth_result}})
    blocked('wrong-run-binding', {**spec, 'runId': 'other'})
    blocked('wrong-role-binding', {**spec, 'role': 'b'})
    blocked('wrong-attempt-binding', {**spec, 'attempt': 2})
    blocked('wrong-task-revision', {**spec, 'taskRevision': 3})
    blocked('wrong-created-revision', {**spec, 'createdTaskRevision': 1})
    blocked('wrong-expected-archive-hash', {**spec, 'expectedArchiveRequestSha': 'different'})
    blocked('wrong-repository', {**spec, 'repository': str(base / 'other')})
    blocked('wrong-worktree', {**spec, 'worktree': str(base / 'other')})

    original_create = create_path.read_bytes()
    create_path.write_text(json.dumps({'content': [{'type': 'text', 'text': json.dumps({**created, 'type': 'queued'})}], 'isError': False}))
    blocked('create-raw-hash-drift', spec)
    create_path.write_bytes(original_create)

    original_archive = archive_path.read_bytes()
    archive_path.write_text(json.dumps({'content': [{'type': 'text', 'text': json.dumps({'type': 'completed'})}], 'isError': False}))
    blocked('archive-raw-hash-drift', spec)
    archive_path.write_bytes(original_archive)

    active_path, _ = complete('completion-active', lambda a: a.list_artifacts(), active)
    blocked('completion-still-active', {**spec, 'completionInventoryOperation': 'completion-active',
                                        'completionInventoryRaw': str(active_path)})
    missing_path, _ = complete('completion-missing', lambda a: a.list_artifacts(), {'artifacts': []})
    blocked('completion-missing-archived', {**spec, 'completionInventoryOperation': 'completion-missing',
                                            'completionInventoryRaw': str(missing_path)})
    bad_root = copy.deepcopy(archived)
    bad_root['artifacts'][0]['payload']['worktree']['root'] = str(base / 'other')
    bad_root_path, _ = complete('completion-bad-root', lambda a: a.list_artifacts(), bad_root)
    blocked('completion-wrong-root', {**spec, 'completionInventoryOperation': 'completion-bad-root',
                                      'completionInventoryRaw': str(bad_root_path)})
    bad_host = copy.deepcopy(archived)
    bad_host['artifacts'][0]['payload']['worktree']['hostId'] = 'remote'
    bad_host_path, _ = complete('completion-bad-host', lambda a: a.list_artifacts(), bad_host)
    blocked('completion-wrong-host', {**spec, 'completionInventoryOperation': 'completion-bad-host',
                                      'completionInventoryRaw': str(bad_host_path)})
    bad_source = copy.deepcopy(archived)
    bad_source['artifacts'][0]['payload']['worktree']['sourceGitRoot'] = str(base / 'other')
    bad_source_path, _ = complete('completion-bad-source', lambda a: a.list_artifacts(), bad_source)
    blocked('completion-wrong-source', {**spec, 'completionInventoryOperation': 'completion-bad-source',
                                        'completionInventoryRaw': str(bad_source_path)})
    bad_archive_path, _ = complete(
        'archive-unknown', lambda a: a.archive_worktree(str(worktree)), {'type': 'unknown'}, archive=True)
    blocked('unknown-archive-result', {**spec, 'archiveOperation': 'archive-unknown',
                                       'archiveRaw': str(bad_archive_path),
                                       'expectedArchiveRequestSha': adapter('archive-unknown', archive=True).journal.inspect('archive-unknown')['requestSha']})
    blocked('stale-completion', spec, now=datetime.now(timezone.utc) + timedelta(seconds=31))

    # 目录先被外部删除但 Git 注册仍在时，不能把队列回执当成清理完成。
    git(repo, 'worktree', 'add', str(worktree), 'codex/archive-guard')
    shutil.rmtree(worktree)
    blocked('git-registration-remains', spec)

    manifest = base / 'manifest.json'
    manifest.write_text(json.dumps({**spec, 'worktree': str(worktree)}))
    cli = subprocess.run([sys.executable, str(project / 'scripts/host_archive_guard.py'), str(manifest)],
                         text=True, capture_output=True, timeout=30)
    check('blocked-cli-is-nonzero', cli.returncode == 1 and json.loads(cli.stdout)['status'] == 'blocked')

print(json.dumps({'scope': 'controlled_integration', 'realHostValidated': False,
                  'passed': len(checks), 'checks': checks}, ensure_ascii=False))
