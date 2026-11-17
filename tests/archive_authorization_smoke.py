"""本地归档授权信封的临时 Git/fixture 测试；不调用宿主、不删除资源。"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from archive_authorization import build_envelope, verify_envelope
from cleanup_apply import preview

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def rejected(name, call):
    try:
        call()
    except (ValueError, KeyError, TypeError, OSError):
        checks.append(name)
    else:
        raise AssertionError(name)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()


with tempfile.TemporaryDirectory(prefix='threadweave-authorization-') as td:
    base = Path(td).resolve()
    repo = base / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Validation')
    git(repo, 'config', 'user.email', 'validation@example.invalid')
    (repo / 'tracked').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    worktree = base / 'worker'
    git(repo, 'worktree', 'add', '-b', 'codex/authorization', str(worktree))
    report = base / 'report.md'
    report.write_text('accepted')
    evidence_path = base / 'evidence.json'
    thread_id = 'thread-authorization'
    run_id = 'authorization-fixture'
    stamp = datetime.now(timezone.utc)
    observed = (stamp - timedelta(seconds=1)).isoformat()
    declaration = {
        'runId': run_id,
        'repository': str(repo),
        'worktree': str(worktree),
        'branch': 'codex/authorization',
        'head': git(worktree, 'rev-parse', 'HEAD'),
        'integrationRef': 'main',
        'report': str(report),
        'reportSha256': hashlib.sha256(report.read_bytes()).hexdigest(),
        'evidence': str(evidence_path),
    }
    evidence = {
        'observedAt': observed,
        'runId': run_id,
        'worktree': str(worktree),
        'branch': declaration['branch'],
        'head': declaration['head'],
        'reportSha256': declaration['reportSha256'],
        'ownedByRun': True,
        'shared': False,
        'acceptanceVerified': True,
        'executionKind': 'host_thread',
        'threadId': thread_id,
        'stop': {'threadIdle': True, 'finalAnswerPresent': True, 'noActiveTurn': True},
    }
    evidence_path.write_text(json.dumps(evidence))
    approved = preview(declaration)
    binding = {'runId': run_id, 'role': 'a', 'attempt': 1, 'taskRevision': 2}
    stop = {
        **binding,
        'source': 'desktop_tool_bridge',
        'operationId': 'wait-stop',
        'observedAt': observed,
        'metadata': {'hostId': 'local', 'result': {'polls': [{
            'thread': {'id': thread_id, 'hostId': 'local', 'status': {'type': 'idle'}},
            'latestTurn': {'id': 'turn-1', 'status': 'completed'},
            'latestAssistantMessage': {'turnId': 'turn-1', 'phase': 'final_answer'},
        }]}},
    }
    inventory = {
        **binding,
        'source': 'desktop_tool_bridge',
        'operationId': 'inventory-pre',
        'observedAt': observed,
        'metadata': {'hostId': 'local', 'result': {'artifacts': [{
            'attachmentType': 'worktree',
            'identityKey': str(worktree),
            'payload': {'hostId': 'local', 'root': str(worktree),
                        'workspaceRoot': str(worktree)},
        }]}},
    }
    manifest = {
        'declaration': declaration,
        'approvedPlan': approved,
        'role': 'a',
        'attempt': 1,
        'taskRevision': 2,
        'stopReceipt': stop,
        'preInventoryReceipt': inventory,
        'archiveRequestSha': 'a' * 64,
        'authorizationNonce': 'authorization-fixture-nonce',
        'authorizedAt': stamp.isoformat(),
        'expiresAt': (stamp + timedelta(seconds=120)).isoformat(),
    }
    result = build_envelope(manifest, now=stamp)
    check('envelope-built', result['status'] == 'envelope_prepared')
    check('local-binding-not-host-signature', result['localAuthorizationBindingVerified'] is True
          and result['hostSignedIdentityVerified'] is False
          and result['userApprovalCryptographicallyVerified'] is False)
    check('never-cleanup-eligible', result['cleanupEligible'] is False
          and result['deletionPerformed'] is False)
    check('binds-plan-and-evidence', result['envelope']['cleanupPlanFingerprint'] == approved['fingerprint']
          and result['envelope']['cleanupEvidenceSha256'] == approved['plan']['evidenceSha256'])
    check('binds-stop-inventory-request', result['envelope']['stopReceiptSha256']
          and result['envelope']['preInventoryReceiptSha256']
          and result['envelope']['archiveRequestSha'] == 'a' * 64)
    verified = verify_envelope(manifest, result, now=stamp)
    check('envelope-verified', verified['status'] == 'envelope_verified'
          and verified['authorizationFingerprint'] == result['authorizationFingerprint'])

    changed_stop = copy.deepcopy(stop)
    changed_stop['metadata']['result']['polls'][0]['thread']['status']['type'] = 'active'
    rejected('active-stop-blocked',
             lambda: build_envelope({**manifest, 'stopReceipt': changed_stop}, now=stamp))
    changed_inventory = copy.deepcopy(inventory)
    changed_inventory['metadata']['result']['artifacts'][0]['attachmentType'] = 'archived_worktree'
    rejected('archived-pre-inventory-blocked',
             lambda: build_envelope({**manifest, 'preInventoryReceipt': changed_inventory}, now=stamp))
    rejected('invalid-archive-sha',
             lambda: build_envelope({**manifest, 'archiveRequestSha': 'not-a-sha'}, now=stamp))
    rejected('ttl-too-long',
             lambda: build_envelope({**manifest, 'expiresAt': (stamp + timedelta(seconds=301)).isoformat()}, now=stamp))
    rejected('expired-window',
             lambda: build_envelope(manifest, now=stamp + timedelta(seconds=121)))
    rejected('plan-drift',
             lambda: build_envelope({**manifest, 'approvedPlan': {**approved, 'fingerprint': 'b' * 64}}, now=stamp))
    changed_declaration = copy.deepcopy(manifest)
    changed_declaration['declaration']['branch'] = 'other-branch'
    rejected('declaration-drift',
             lambda: build_envelope(changed_declaration, now=stamp))
    rejected('missing-authorization-nonce',
             lambda: build_envelope({**manifest, 'authorizationNonce': ''}, now=stamp))
    tampered = copy.deepcopy(result)
    tampered['envelope']['threadId'] = 'other-thread'
    rejected('tampered-envelope', lambda: verify_envelope(manifest, tampered, now=stamp))
    rejected('verify-changed-manifest',
             lambda: verify_envelope({**manifest, 'archiveRequestSha': 'b' * 64}, result, now=stamp))

    manifest_path = base / 'manifest.json'
    envelope_path = base / 'envelope.json'
    manifest_path.write_text(json.dumps(manifest))
    envelope_path.write_text(json.dumps(result))
    cli = subprocess.run([
        sys.executable, str(project / 'scripts/archive_authorization.py'),
        'verify', str(manifest_path), '--envelope', str(envelope_path),
    ], text=True, capture_output=True, timeout=30)
    check('cli-verify-success', cli.returncode == 0
          and json.loads(cli.stdout)['status'] == 'envelope_verified')
    cli = subprocess.run([
        sys.executable, str(project / 'scripts/archive_authorization.py'),
        'build', str(manifest_path),
    ], text=True, capture_output=True, timeout=30)
    check('cli-build-success', cli.returncode == 0
          and json.loads(cli.stdout)['status'] == 'envelope_prepared')

print(json.dumps({'scope': 'controlled_integration', 'hostInvocation': False,
                  'passed': len(checks), 'checks': checks}, ensure_ascii=False))
